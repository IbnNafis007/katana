# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members
import json

from twisted.internet import defer
from twisted.python import log

from twisted.web import html
from twisted.web.util import Redirect
from twisted.web.resource import NoResource
from buildbot.status.web.base import HtmlResource, \
    BuildLineMixin, ActionResource, path_to_slave, path_to_authzfail, path_to_json_slaves, \
    path_to_json_past_slave_builds, absolute_path_to_slave
from buildbot.status.web.status_json import FilterOut, PastBuildsJsonResource, SlaveJsonResource


class ShutdownActionResource(ActionResource):
    def __init__(self, slave):
        self.slave = slave
        self.action = "gracefulShutdown"

    @defer.inlineCallbacks
    def performAction(self, request):
        authz = self.getAuthz(request)
        res = yield authz.actionAllowed(self.action, request, self.slave)

        url = None
        if res:
            username = authz.getUsernameFull(request)
            slave_url = absolute_path_to_slave(self.slave.master.status, self.slave)
            log.msg("Shutdown %s gracefully requested by %s" % (self.slave.name, username))
            self.slave.master.status.slaveShutdownGraceFully(self.slave.name, slave_url, username)
            self.slave.setGraceful(True)
            url = path_to_slave(request, self.slave)
        else:
            url = path_to_authzfail(request)
        defer.returnValue(url)


class PauseActionResource(ActionResource):

    def __init__(self, slave, state):
        self.slave = slave
        self.action = "pauseSlave"
        self.state = state

    @defer.inlineCallbacks
    def performAction(self, request):
        authz = self.getAuthz(request)
        res = yield authz.actionAllowed(self.action, request, self.slave)

        url = None
        if res:
            username = authz.getUsernameFull(request)
            slave_url = absolute_path_to_slave(self.slave.master.status, self.slave)
            if self.slave.isPaused():
                self.slave.master.status.slaveUnpaused(self.slave.name, slave_url, username)
                action = "Unpause"
            else:
                self.slave.master.status.slavePaused(self.slave.name, slave_url, username)
                action = "Pause"
            log.msg("%s %s requested by %s" % (action, self.slave.name, username))
            self.slave.setPaused(self.state)
            url = path_to_slave(request, self.slave)
        else:
            url = path_to_authzfail(request)
        defer.returnValue(url)


# /buildslaves/$slavename
class OneBuildSlaveResource(HtmlResource, BuildLineMixin):
    addSlash = False

    def __init__(self, slavename):
        HtmlResource.__init__(self)
        self.slavename = slavename

    def getPageTitle(self, req):
        return "%s" % self.slavename

    def getChild(self, path, req):
        s = self.getStatus(req)
        slave = s.getSlave(self.slavename)
        if path == "shutdown":
            return ShutdownActionResource(slave)
        if path == "pause" or path == "unpause":
            return PauseActionResource(slave, path == "pause")
        return Redirect(path_to_slave(req, slave))

    @defer.inlineCallbacks
    def content(self, request, ctx):
        s = self.getStatus(request)
        slave_status = s.getSlave(self.slavename)

        try:
            max_builds = int(request.args.get('numbuilds')[0])
        except:
            max_builds = 15

        filters = {
            "slave": self.slavename
        }

        bbURL = s.getBuildbotURL()

        slave_params = {
            "build_steps": ["0"],
            "build_props": ["0"],
            "builders": ["0"]
        }
        slave_json = SlaveJsonResource(s, slave_status)
        slave_dict = slave_json.asDict(request, params=slave_params)
        slave_url = bbURL + path_to_json_slaves(self.slavename)
        ctx['instant_json']['slave'] = {"url": slave_url,
                                                 "data": json.dumps(slave_dict, separators=(',', ':')),
                                                 "waitForPush": s.master.config.autobahn_push,
                                                 "pushFilters": {
                                                     "buildStarted": filters,
                                                     "buildFinished": filters,
                                                     "stepStarted": filters,
                                                     "stepFinished": filters,
                                                 }}

        recent_builds_json = PastBuildsJsonResource(s, max_builds, slave_status=slave_status)
        recent_builds_dict = yield recent_builds_json.asDict(request)
        recent_builds_url = bbURL + path_to_json_past_slave_builds(request, self.slavename, max_builds)
        ctx['instant_json']['recent_builds'] = {"url": recent_builds_url,
                                                "data": json.dumps(recent_builds_dict, separators=(',', ':')),
                                                "waitForPush": s.master.config.autobahn_push,
                                                "pushFilters": {
                                                    "buildStarted": filters,
                                                    "buildFinished": filters,
                                                }}

        # connects over the last hour
        slave = s.getSlave(self.slavename)

        if slave:
            connect_count = slave.getConnectCount()
            if slave.isPaused():
                pause_url = request.childLink("unpause")
            else:
                pause_url = request.childLink("pause")
            ctx.update(dict(slave=slave,
                        slavename=slave.getFriendlyName(),
                        shutdown_url=request.childLink("shutdown"),
                        pause_url = pause_url,
                        authz=self.getAuthz(request),
                        this_url="../../../" + path_to_slave(request, slave),
                        access_uri=slave.getAccessURI()),
                   admin=unicode(slave.getAdmin() or '', 'utf-8'),
                   host=unicode(slave.getHost() or '', 'utf-8'),
                   slave_version=slave.getVersion(),
                   show_builder_column=True,
                   connect_count=connect_count)
        else:
            ctx.update(dict(slavename=self.slavename,
                            shutdown_url=request.childLink("shutdown")))

        template = request.site.buildbot_service.templates.get_template("buildslave.html")
        data = template.render(**ctx)
        defer.returnValue(data)


# /buildslaves
class BuildSlavesResource(HtmlResource):
    pageTitle = "Build slaves"
    addSlash = True

    def content(self, request, cxt):
        s = self.getStatus(request)

        slave_params = {
            "build_steps": ["0"],
            "build_props": ["0"],
            "builders": ["0"]
        }

        slaves = s.getSlaves()
        slaves_array = [SlaveJsonResource(s, ss.slave_status).asDict(request, params=slave_params)
                        for ss in slaves.values()]
        slaves_dict = FilterOut(slaves_array)

        cxt['instant_json']["slaves"] = {"url": s.getBuildbotURL() + path_to_json_slaves(),
                                         "data": json.dumps(slaves_dict, separators=(',', ':')),
                                         "waitForPush": s.master.config.autobahn_push,
                                         "pushFilters": {
                                             "buildStarted": {},
                                             "buildFinished": {},
                                             "stepStarted": {},
                                             "stepFinished": {},
                                             "slaveConnected": {},
                                             "slaveDisconnected": {},
                                         }}

        template = request.site.buildbot_service.templates.get_template("buildslaves.html")
        return template.render(**cxt)

    def getChild(self, path, req):
        try:
            self.getStatus(req).getSlave(path)
            return OneBuildSlaveResource(path)
        except KeyError:
            return NoResource("No such slave '%s'" % html.escape(path))

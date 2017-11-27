import json
import time

from twisted.application import service
from twisted.internet import reactor, defer
from twisted.python import log

from buildbot import config
from buildbot.util.lru import AsyncLRUCache


class PropertiesDict(dict):
    """
    LRUCache cannot handle pure dicts due to weakref issues, so
    we have to create this bogus class
    """
    pass


class BuildRequestMerger(config.ReconfigurableServiceMixin, service.Service):

    # Basic list of properties that must match for a buildrequest to be merged
    # BuilderConfigs can define additional properties (see _propertiesMatch)
    BASE_MERGE_PROPERTIES = ['force_rebuild', 'force_chain_rebuild']

    def __init__(self, master):
        self.master = master
        self.properties_cache = AsyncLRUCache(
            miss_fn=None,  # Cache contents are handled manually
            max_size=20000)

        # lock to indicate that merged builds are being added
        self.build_merging_lock = defer.DeferredLock()


    @defer.inlineCallbacks
    def addBuildset(self,
                    sourcestampsetid,
                    reason,
                    properties,
                    triggeredbybrid=None,
                    builderNames=None,
                    external_idstring=None,
                    _reactor=reactor):
        """
        Wrapper around buildsets.addBuildset that does merging before
        buildrequests hit the db.

        ..seealso:: self._getMergeBrDict
            For more documentation on merge conditions

        ..seealso:: buildsets.addBuildset
            For parameter details
        """
        builderNames = sorted(builderNames)

        start = time.time()
        buildsetLog = {
            'name': 'addBuildset',
            'description':
            'Log merges within a chain while adding new buildsets',
            'sourcestampsetid': sourcestampsetid,
            'builderNames': builderNames,
        }
        # For every builderName in this buildset, check which ones can be merged
        brDictsToMerge = {}

        # Don't read sourcestamp information yet, since we might not need it
        sourcestamps = None

        for builderName in builderNames:
            builderMergeStart = time.time()

            if 'selected_slave' in properties:
                # Never merge if a build request has a selected_slave
                # This might happen when a user wants to test the same build in different
                # slaves to look for instabilities
                mergeBrDict = None
            else:
                # Look for a builder that matches the configured properties
                mergeProperties = self.master.botmaster.builders[
                    builderName].config.mergeProperties

                # And sourcestamps (only need to read them once)
                if sourcestamps is None:
                    sourcestamps = yield self.master.db.sourcestamps.getSimpleSourceStamps(
                        sourcestampsetid)

                mergeBrDict = yield self._getMergeBrDict(
                    builderName, sourcestamps, properties, mergeProperties)

            # If we found one, add it to our merge map
            if mergeBrDict:
                brDictsToMerge[builderName] = mergeBrDict

            buildsetLog[builderName] = {
                'elapsed': time.time() - builderMergeStart,
                'mergeBrid':
                brDictsToMerge.get(builderName, {}).get('brid', None)
            }

        buildsetLog['elapsed_merge'] = time.time() - start

        # Finally add the buildset passing the map of `brDictsToMerge`
        # This method will make sure that all new breqs will enter the db
        # marked as merged, and will not run.
        _master_objectid = yield self.master.getObjectId()

        if brDictsToMerge:
            yield self.build_merging_lock.acquire()
        try:
            result = yield self.master.db.buildsets.addBuildset(
                sourcestampsetid=sourcestampsetid,
                reason=reason,
                properties=properties,
                triggeredbybrid=triggeredbybrid,
                builderNames=builderNames,
                brDictsToMerge=brDictsToMerge,
                external_idstring=external_idstring,
                _reactor=_reactor,
                _master_objectid=_master_objectid)
        finally:
            if brDictsToMerge:
                self.build_merging_lock.release()

        # Log more ids
        (bsid, brids) = result
        buildsetLog['buildsetid'] = bsid
        for builderName, brid in brids.iteritems():
            buildsetLog[builderName]['brid'] = brid
        buildsetLog['elapsed_total'] = time.time() - start
        log.msg(json.dumps(buildsetLog))

        defer.returnValue(result)

    @defer.inlineCallbacks
    def _getMergeBrDict(self, builderName, sourcestamps, properties,
                        mergeProperties):
        """
        Looks for a buildrequest we can merge into.

        It must match `builderName`, `sourcestamps` and all properties defined
        in `mergeProperties`.

        This will only merge against builds that have already been claimed
        and are currently running (unfinished builds).

        :return BrDict or None:
            Buildrequest dictionary for a request that can be merged into.
            `None` if no match was found.
        """
        matchingBrDicts = yield self.master.db.buildrequests.getBuildRequests(
            buildername=builderName,
            complete=False,
            claimed=True,
            sourcestamps=sourcestamps,
            mergebrids="exclude")

        # Get properties for matching breqs (done in a single query for optimization)
        otherProperties = yield self._getBuildsetsProperties(
            [brdict['buildsetid'] for brdict in matchingBrDicts])

        # Check if relevant properties match
        for brdict in matchingBrDicts:
            if self._propertiesMatch(properties,
                                     otherProperties[brdict['buildsetid']],
                                     mergeProperties):

                # If they match, fetch the build number and merge against this buildrequest
                brdict[
                    'build_number'] = yield self.master.db.builds.getBuildNumberForRequest(
                        brdict['brid'])
                defer.returnValue(brdict)
                return

        # If we can't find any match, return None
        defer.returnValue(None)

    def _propertiesMatch(self, properties, otherProperties, mergeProperties):
        """
        :param dict(str,str) properties:
        :param dict(str,str) otherProperties:
        :param list(str) mergeProperties:
            List of properties that must match
        :return bool:
            True if `properties` and `otherProperties` match for a list of
            `mergeProperties` to be checked
        """
        if 'selected_slave' in otherProperties:
            return False

        for propertyName in self.BASE_MERGE_PROPERTIES + mergeProperties:
            if properties.get(propertyName, None) != otherProperties.get(
                    propertyName, None):
                return False

        return True

    @defer.inlineCallbacks
    def _getBuildsetsProperties(self, buildsetids):
        properties = {}

        # Fill in properties from cache
        for buildsetid in buildsetids:
            if buildsetid in self.properties_cache.cache:
                properties[buildsetid] = yield self.properties_cache.get(
                    buildsetid)

        # Read missing properties in a single query
        missing_buildsetids = set(buildsetids).difference(properties.keys())
        if missing_buildsetids:
            db_properties = yield self.master.db.buildsets.getBuildsetsProperties(
                missing_buildsetids)
            properties.update(db_properties)

        # Populate cache with new values
        for buildsetid in missing_buildsetids:
            bs_properties = PropertiesDict(properties[buildsetid])
            self.properties_cache.put_new(buildsetid, bs_properties)

        defer.returnValue(properties)

from distutils.version import StrictVersion

try:
    import simplejson as json
except ImportError:
    import json

from release.info import getProductDetails
from release.paths import makeCandidatesDir
from release.platforms import buildbot2updatePlatforms, buildbot2bouncer, \
  buildbot2ftp
from release.versions import getPrettyVersion
from balrog.submitter.api import Release, SingleLocale, Rule
from util.algorithms import recursive_update


def get_nightly_blob_name(productName, branch, build_type, suffix, dummy=False):
    if dummy:
        branch = '%s-dummy' % branch
    return '%s-%s-%s-%s' % (productName, branch, build_type, suffix)


def get_release_blob_name(productName, version, build_number, dummy=False):
    name = '%s-%s-build%s' % (productName, version, build_number)
    if dummy:
        name += '-dummy'
    return name


class ReleaseCreatorBase(object):
    def __init__(self, api_root, auth, dummy=False):
        self.api_root = api_root
        self.auth = auth
        self.dummy = dummy

    def generate_data(self, appVersion, productName, version, buildNumber,
                      updateChannels, stagingServer, bouncerServer,
                      enUSPlatforms, schemaVersion, openURL=None,
                      **updateKwargs):
        assert schemaVersion in (2, 3), 'Unhandled schema version %s' % schemaVersion
        self.name = get_release_blob_name(productName, version, buildNumber)
        data = {
            'name': self.name,
            'detailsUrl': getProductDetails(productName.lower(), appVersion),
            'platforms': {},
            'fileUrls': {},
            'ftpFilenames': {},
            'bouncerProducts': {},
        }
        data['appVersion'] = appVersion
        data['platformVersion'] = appVersion
        data['displayVersion'] = getPrettyVersion(version)

        actions = []
        if openURL:
            actions.append("showURL")
            data["openURL"] = openURL

        if actions:
            data["actions"] = " ".join(actions)

        # XXX: This is a hack for bug 1045583. We should remove it, and always
        # use "candidates" for nightlyDir after the switch to Balrog is complete.
        if productName.lower() == "mobile":
            nightlyDir = "candidates"
        else:
            nightlyDir = "nightly"

        for channel in updateChannels:
            if channel in ('betatest', 'esrtest') or "localtest" in channel:
                dir_ = makeCandidatesDir(productName.lower(), version,
                                         buildNumber, server=stagingServer, protocol='http',
                                         nightlyDir=nightlyDir)
                data['fileUrls'][channel] = '%supdate/%%OS_FTP%%/%%LOCALE%%/%%FILENAME%%' % dir_
            else:
                url = 'http://%s/?product=%%PRODUCT%%&os=%%OS_BOUNCER%%&lang=%%LOCALE%%' % bouncerServer
                data['fileUrls'][channel] = url

        # XXX: quick hack for bug 1021026. We should be using Bouncer for this
        # after we implement better solution talked about in comments 2 through 4
        if channel == 'release':
            dir_ = makeCandidatesDir(productName.lower(), version,
                                     buildNumber, server='download.cdn.mozilla.net', protocol='http',
                                     nightlyDir=nightlyDir)
            url = '%supdate/%%OS_FTP%%/%%LOCALE%%/%%FILENAME%%' % dir_
            data['fileUrls']['beta'] = url
            data['fileUrls']['beta-cdntest'] = url

        data.update(self._get_update_data(productName, version, **updateKwargs))

        for platform in enUSPlatforms:
            updatePlatforms = buildbot2updatePlatforms(platform)
            bouncerPlatform = buildbot2bouncer(platform)
            ftpPlatform = buildbot2ftp(platform)
            data['platforms'][updatePlatforms[0]] = {
                'OS_BOUNCER': bouncerPlatform,
                'OS_FTP': ftpPlatform
            }
            for aliasedPlatform in updatePlatforms[1:]:
                data['platforms'][aliasedPlatform] = {
                    'alias': updatePlatforms[0]
                }

        return data

    def run(self, appVersion, productName, version, buildNumber,
            updateChannels, stagingServer, bouncerServer,
            enUSPlatforms, hashFunction, schemaVersion, openURL=None,
            **updateKwargs):
        api = Release(auth=self.auth, api_root=self.api_root)
        data = self.generate_data(appVersion, productName, version,
                                  buildNumber, updateChannels,
                                  stagingServer, bouncerServer, enUSPlatforms,
                                  schemaVersion, openURL, **updateKwargs)
        current_data, data_version = api.get_data(self.name)
        data = recursive_update(current_data, data)
        api = Release(auth=self.auth, api_root=self.api_root)
        api.update_release(name=self.name,
                           version=appVersion,
                           product=productName,
                           hashFunction=hashFunction,
                           releaseData=json.dumps(data),
                           data_version=data_version)


class ReleaseCreatorV2(ReleaseCreatorBase):
    def run(self, *args, **kwargs):
        return ReleaseCreatorBase.run(self, *args, schemaVersion=2, **kwargs)

    def _get_update_data(self, productName, version, partialUpdates):
        data = {}
        previousVersion = str(max(StrictVersion(v) for v in partialUpdates))

        data['ftpFilenames']['complete'] = '%s-%s.complete.mar' % (productName.lower(), version)
        data['ftpFilenames']['partial'] = '%s-%s-%s.partial.mar' % (productName.lower(), previousVersion, version)
        data['bouncerProducts']['complete'] = '%s-%s-complete' % (productName.lower(), version)
        data['bouncerProducts']['partial'] = '%s-%s-partial-%s' % (productName.lower(), version, previousVersion)

        return data


class ReleaseCreatorV3(ReleaseCreatorBase):
    def run(self, *args, **kwargs):
        return ReleaseCreatorBase.run(self, *args, schemaVersion=3, **kwargs)

    def _get_update_data(self, productName, version, partialUpdates):
        data = {
            "ftpFilenames": {
                "completes": {
                    "*": "%s-%s.complete.mar" % (productName.lower(), version),
                }
            },
            "bouncerProducts": {
                "completes": {
                    "*": "%s-%s-complete" % (productName.lower(), version),
                }
            }
        }

        if partialUpdates:
            data["ftpFilenames"]["partials"] = {}
            data["bouncerProducts"]["partials"] = {}
            for previousVersion, previousInfo in partialUpdates.iteritems():
                from_ = get_release_blob_name(productName, previousVersion,
                                              previousInfo["buildNumber"],
                                              self.dummy)
                filename = "%s-%s-%s.partial.mar" % (productName.lower(), previousVersion, version)
                bouncerProduct = "%s-%s-partial-%s" % (productName.lower(), version, previousVersion)
                data["ftpFilenames"]["partials"][from_] = filename
                data["bouncerProducts"]["partials"][from_] = bouncerProduct

        return data


class NightlySubmitterBase(object):
    build_type = 'nightly'

    def __init__(self, api_root, auth, dummy=False):
        self.api_root = api_root
        self.auth = auth
        self.dummy = dummy

    def run(self, platform, buildID, productName, branch, appVersion, locale,
            hashFunction, extVersion, schemaVersion, isOSUpdate=None, **updateKwargs):
        assert schemaVersion in (2, 3), 'Unhandled schema version %s' % schemaVersion
        targets = buildbot2updatePlatforms(platform)
        build_target = targets[0]
        alias = None
        if len(targets) > 1:
            alias = targets[1:]

        data = {
            'buildID': buildID,
        }
        data['appVersion'] = appVersion
        data['platformVersion'] = extVersion
        data['displayVersion'] = appVersion
        if isOSUpdate:
            data['isOSUpdate'] = isOSUpdate

        data.update(self._get_update_data(productName, branch, **updateKwargs))

        # Bug 1055305 - a hack so that we can have JB and KK OTA for flame.
        # They both query with buildTarget of flame, but differ in OS Version,
        # so we need separate release blobs and rule to do the right thing
        build_type = self.build_type
        if build_target == 'flame-kk':
            build_type = 'kitkat-%s' % build_type

        name = get_nightly_blob_name(productName, branch, build_type, buildID, self.dummy)
        data = json.dumps(data)
        api = SingleLocale(auth=self.auth, api_root=self.api_root)
        copyTo = [get_nightly_blob_name(
            productName, branch, build_type, 'latest', self.dummy)]
        copyTo = json.dumps(copyTo)
        alias = json.dumps(alias)
        api.update_build(name=name, product=productName,
                         build_target=build_target,
                         version=appVersion, locale=locale,
                         hashFunction=hashFunction,
                         buildData=data, copyTo=copyTo, alias=alias,
                         schemaVersion=schemaVersion)


class NightlySubmitterV2(NightlySubmitterBase):
    def run(self, *args, **kwargs):
        return NightlySubmitterBase.run(self, *args, schemaVersion=2, **kwargs)

    def _get_update_data(self, productName, branch, completeMarSize,
                         completeMarHash, completeMarUrl, partialMarSize=None,
                         partialMarHash=None, partialMarUrl=None,
                         previous_buildid=None):
        data = {}
        data['complete'] = {
            'from': '*',
            'filesize': completeMarSize,
            'hashValue': completeMarHash,
            'fileUrl': completeMarUrl
        }
        if partialMarSize:
            data['partial'] = {
                'from': get_nightly_blob_name(productName, branch,
                                              self.build_type,
                                              previous_buildid,
                                              self.dummy),
                'filesize': partialMarSize,
                'hashValue': partialMarHash,
                'fileUrl': partialMarUrl
            }

        return data


class NightlySubmitterV3(NightlySubmitterBase):
    def run(self, *args, **kwargs):
        return NightlySubmitterBase.run(self, *args, schemaVersion=3, **kwargs)

    def _get_update_data(self, productName, branch, completeInfo,
                         partialInfo=None):
        data = {"completes": []}

        for info in completeInfo:
            if "from_buildid" in info:
                from_ = get_nightly_blob_name(productName, branch,
                                              self.build_type,
                                              info["from_buildid"],
                                              self.dummy)
            else:
                from_ = "*"
            data["completes"].append({
                "from": from_,
                "filesize": info["size"],
                "hashValue": info["hash"],
                "fileUrl": info["url"],
            })
        if partialInfo:
            data["partials"] = []
            for info in partialInfo:
                data["partials"].append({
                    "from": get_nightly_blob_name(productName, branch,
                                                  self.build_type,
                                                  info["from_buildid"],
                                                  self.dummy),
                    "filesize": info["size"],
                    "hashValue": info["hash"],
                    "fileUrl": info["url"],
                })

        return data


class ReleaseSubmitterBase(object):
    def __init__(self, api_root, auth, dummy=False):
        self.api_root = api_root
        self.auth = auth
        self.dummy = dummy

    def run(self, platform, productName, appVersion, version, build_number, locale,
            hashFunction, extVersion, buildID, schemaVersion, **updateKwargs):
        assert schemaVersion in (2, 3), 'Unhandled schema version %s' % schemaVersion
        targets = buildbot2updatePlatforms(platform)
        # Some platforms may have alias', but those are set-up elsewhere
        # for release blobs.
        build_target = targets[0]

        name = get_release_blob_name(productName, version, build_number,
                                     self.dummy)

        data = {
            'buildID': buildID,
        }
        data['appVersion'] = appVersion
        data['platformVersion'] = extVersion
        data['displayVersion'] = getPrettyVersion(version)

        data.update(self._get_update_data(productName, version, build_number, **updateKwargs))

        data = json.dumps(data)
        api = SingleLocale(auth=self.auth, api_root=self.api_root)
        schemaVersion = json.dumps(schemaVersion)
        api.update_build(name=name, product=productName,
                         build_target=build_target, version=appVersion,
                         locale=locale, hashFunction=hashFunction,
                         buildData=data, schemaVersion=schemaVersion)


class ReleaseSubmitterV2(ReleaseSubmitterBase):
    def run(self, *args, **kwargs):
        return ReleaseSubmitterBase.run(self, *args, schemaVersion=2, **kwargs)

    def _get_update_data(self, productName, version, build_number,
                         completeMarSize, completeMarHash):
        data = {}

        data['complete'] = {
            'from': '*',
            'filesize': completeMarSize,
            'hashValue': completeMarHash,
        }
        # XXX: We never supported partials in schema 2 for releases.

        return data


class ReleaseSubmitterV3(ReleaseSubmitterBase):
    def run(self, *args, **kwargs):
        return ReleaseSubmitterBase.run(self, *args, schemaVersion=3, **kwargs)

    def _get_update_data(self, productName, version, build_number,
                         completeInfo, partialInfo=None):
        data = {"completes": []}

        for info in completeInfo:
            if "previousVersion" in info:
                from_ = get_release_blob_name(productName, version,
                                              build_number, self.dummy)
            else:
                from_ = "*"
            data["completes"].append({
                "from": from_,
                "filesize": info["size"],
                "hashValue": info["hash"],
            })
        if partialInfo:
            data["partials"] = []
            for info in partialInfo:
                data["partials"].append({
                    "from": get_release_blob_name(productName,
                                                  info["previousVersion"],
                                                  info["previousBuildNumber"] ,
                                                  self.dummy),
                    "filesize": info["size"],
                    "hashValue": info["hash"],
                })

        return data


class ReleasePusher(object):
    def __init__(self, api_root, auth, dummy=False):
        self.api_root = api_root
        self.auth = auth
        self.dummy = dummy

    def run(self, productName, version, build_number, rule_ids):
        name = get_release_blob_name(productName, version, build_number,
                                     self.dummy)
        api = Rule(auth=self.auth, api_root=self.api_root)
        for id_ in rule_ids:
            api.update_rule(id_, mapping=name)

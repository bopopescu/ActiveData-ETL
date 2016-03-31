# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import division
from __future__ import unicode_literals

import requests

from pyLibrary import convert
from pyLibrary.debugs.logs import Log, machine_metadata
from pyLibrary.dot import set_default, coalesce, Dict, unwraplist, unwrap, listwrap
from pyLibrary.env import http
from pyLibrary.strings import expand_template
from pyLibrary.testing.fuzzytestcase import assertAlmostEqual
from pyLibrary.times.dates import Date
from testlog_etl import etl2key

DEBUG = True
MAX_THREADS = 5
STATUS_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}"
ARTIFACTS_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}/artifacts"
ARTIFACT_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}/artifacts/{{path}}"
RETRY = {"times": 3, "sleep": 5}
seen = {}

def process(source_key, source, destination, resources, please_stop=None):
    output = []
    etl_source = None

    lines = source.read_lines()
    session = requests.session()
    for i, line in enumerate(lines):
        if please_stop:
            Log.error("Shutdown detected. Stopping early")
        try:
            tc_message = convert.json2value(line)
            taskid = tc_message.status.taskId
            if tc_message.artifact:
                continue
            Log.note("{{id}} w {{artifact}} found (#{{num}})", id=taskid, num=i, artifact=tc_message.artifact.name)

            task = http.get_json(expand_template(STATUS_URL, {"task_id": taskid}), retry=RETRY, session=session)
            normalized = _normalize(tc_message, task)

            # get the artifact list for the taskId
            artifacts = http.get_json(expand_template(ARTIFACTS_URL, {"task_id": taskid}), retry=RETRY).artifacts
            for a in artifacts:
                a.url = expand_template(ARTIFACT_URL, {"task_id": taskid, "path": a.name})
                if a.name.endswith("/live.log"):
                    read_buildbot_properties(normalized, a.url)
            normalized.task.artifacts = artifacts

             # FIX THE ETL
            etl = tc_message.etl
            etl_source = coalesce(etl_source, etl.source)
            etl.source = etl_source
            normalized.etl = set_default(
                {
                    "id": i,
                    "source": etl,
                    "type": "join",
                    "timestamp": Date.now()
                },
                machine_metadata
            )

            tc_message.artifact="." if tc_message.artifact else None
            if normalized.task.id in seen:
                try:
                    assertAlmostEqual([tc_message, task, artifacts], seen[normalized.task.id], places=11)
                except Exception, e:
                    Log.error("Not expected", cause=e)
            else:
                tc_message._meta = None
                tc_message.etl = None
                seen[normalized.task.id] = [tc_message, task, artifacts]

            output.append(normalized)
        except Exception, e:
            Log.warning("problem", cause=e)

    keys = destination.extend({"id": etl2key(t.etl), "value": t} for t in output)
    return keys


def read_buildbot_properties(normalized, url):
    pass
    # response = http.get(url)
    #
    # lines = list(response.all_lines)
    # for l in response.all_lines:
    #     pass



def _normalize(tc_message, task):
    output = Dict()
    set_default(task, tc_message.status)

    output.task.id = task.taskId
    output.task.created = Date(task.created)
    output.task.deadline = Date(task.deadline)
    output.task.dependencies = unwraplist(task.dependencies)
    output.task.env = task.payload.env
    output.task.expires = Date(task.expires)

    if isinstance(task.payload.image, basestring):
        output.task.image = {"path": task.payload.image}

    output.task.priority = task.priority
    output.task.privisioner.id = task.provisionerId
    output.task.retries.remaining = task.retriesLeft
    output.task.retries.total = task.retries
    output.task.routes = task.routes
    output.task.runs = map(_normalize_run, task.runs)
    output.task.run = _normalize_run(task.runs[tc_message.runId])
    if output.task.run.id != tc_message.runId:
        Log.error("not expected")

    output.task.scheduler.id = task.schedulerId
    output.task.scopes = task.scopes
    output.task.state = task.state
    output.task.group.id = task.taskGroupId
    output.task.version = tc_message.version
    output.task.worker.group = tc_message.workerGroup
    output.task.worker.id = tc_message.workerId
    output.task.worker.type = task.workerType

    output.task.artifacts = unwraplist(_object_to_array(task.payload.artifacts, "name"))
    output.task.cache = unwraplist(_object_to_array(task.payload.cache, "name", "path"))
    output.task.command = " ".join(map(convert.string2quote, map(unicode.strip, task.payload.command)))

    output.task.tags = get_tags(task)

    set_build_info(output, task)
    set_run_info(output, task)
    output.build.type = set(listwrap(output.build.type))

    return output


def _normalize_run(run):
    output = Dict()
    output.reason_created = run.reasonCreated
    output.id = run.runId
    output.scheduled = Date(run.scheduled)
    output.started = Date(run.started)
    output.state = run.state
    output.deadline = Date(run.takenUntil)
    output.worker.group = run.workerGroup
    output.worker.id = run.workerId
    return output


def set_run_info(normalized, task):
    """
    Get the run object that contains properties that describe the run of this job
    :param task: The task definition
    :return: The run object
    """
    set_default(
        normalized,
        {"run": {
            "machine": task.extra.treeherder.machine,
            "suite": task.extra.suite,
            "chunk": task.extra.chunks.current
        }}
    )


def set_build_info(normalized, task):
    """
    Get a build object that describes the build
    :param task: The task definition
    :return: The build object
    """
    triple = (task.workerType, task.extra.build_name, task.extra.treeherder.build.platform)
    try:
        set_default(normalized, KNOWN_BUILD_NAMES[triple])
    except Exception:
        KNOWN_BUILD_NAMES[triple] = {}
        Log.warning("Can not find {{triple|json}}", triple=triple)

    set_default(
        normalized,
        {"build": {
            "platform": task.extra.treeherder.build.platform,
            # MOZILLA_BUILD_URL looks like this:
            # "https://queue.taskcluster.net/v1/task/e6TfNRfiR3W7ZbGS6SRGWg/artifacts/public/build/target.tar.bz2"
            "url": task.payload.env.MOZILLA_BUILD_URL,
            "name": task.extra.build_name,
            "product": coalesce(task.extra.treeherder.productName, task.extra.build_product),
            "revision": task.payload.env.GECKO_HEAD_REV,
            "type": [{"dbg": "debug"}.get(task.extra.build_type, task.extra.build_type)]
        }}
    )

    if task.extra.treeherder.collection.opt:
        normalized.build.type += ["opt"]
    elif task.extra.treeherder.collection.debug:
        normalized.build.type += ["debug"]

    # head_repo will look like "https://hg.mozilla.org/try/"
    head_repo = task.payload.env.GECKO_HEAD_REPOSITORY
    branch = head_repo.split("/")[-2]
    normalized.build.branch = branch

    normalized.build.revision12 = normalized.build.revision[0:12]


def get_tags(task):
    tags = [{"name": k, "value": v} for k, v in task.tags.leaves()] + [{"name": k, "value": v} for k, v in task.metadata.leaves()] + [{"name": k, "value": v} for k, v in task.extra.leaves()]
    for t in tags:
        if t["name"] not in KNOWN_TAGS:
            Log.warning("unknown task tag {{tag|quote}}", tag=t["name"])
            KNOWN_TAGS.add(t["name"])

    return unwraplist(tags)


def _object_to_array(value, key_name, value_name=None):
    try:
        if value_name==None:
            return [set_default(v, {key_name: k}) for k, v in value.items()]
        else:
            return [{key_name: k, value_name: v} for k, v in value.items()]
    except Exception, e:
        Log.error("unexpected", cause=e)


KNOWN_TAGS = {
    "build_name",
    "build_type",
    "build_product",
    "description",
    "chunks.current",
    "chunks.total",
    "createdForUser",
    "extra.build_product",  # error?
    "funsize.partials",
    "github.events",
    "github.env",
    "github.headBranch",
    "github.headRepo",
    "github.headRevision",
    "github.headUser",
    "github.baseBranch",
    "github.baseRepo",
    "github.baseUser",

    "index.rank",
    "locations.mozharness",
    "locations.test_packages",
    "locations.build",
    "locations.sources",
    "locations.symbols",
    "locations.tests",
    "name",
    "owner",
    "signing.signature",
    "source",
    "suite.flavor",
    "suite.name",
    "treeherderEnv",
    "treeherder.build.platform",
    "treeherder.collection.debug",
    "treeherder.collection.opt",
    "treeherder.groupSymbol",
    "treeherder.groupName",
    "treeherder.machine.platform",
    "treeherder.productName",
    "treeherder.revision",
    "treeherder.revision_hash",
    "treeherder.symbol",
    "treeherder.tier",
    "url.busybox",
    "useCloudMirror"
}

# MAP TRIPLE (workerType, extra.build_name, extra.treeherder.build.platform)
# TO PROPERTIES
KNOWN_BUILD_NAMES = {
    ("android-api-15", "android-api-15-b2gdroid", "b2gdroid-4-0-armv7-api15"): {},
    ("android-api-15", "android-api-15-gradle-dependencies", "android-4-0-armv7-api15"): {},
    ("android-api-15", "android-lint", "android-4-0-armv7-api15"): {"build": {"platform": "lint"}},
    ("android-api-15", "android-api-15-partner-sample1", "android-4-0-armv7-api15-partner1"): {"run": {"machine": {"os": "android"}}},
    ("android-api-15", "android", "android-4-0-armv7-api15"): {"run": {"machine": {"os": "android"}}},
    ("android-api-15", "android-api-15-frontend", "android-4-0-armv7-api15"): {"run": {"machine": {"os": "android"}}},
    ("b2gtest", "mozharness-tox", "lint"): {},
    # ("b2gtest", "marionette-harness-pytest", "linux64"): {},
    ("b2gtest", None, None): {},
    ("b2gtest", None, "mulet-linux64"): {"build": {"platform": "linux64", "type": ["mulet"]}},
    ("b2gtest", "", "lint"): {"build": {"platform": "lint"}},
    ("b2gtest", "eslint-gecko", "lint"): {"build": {"platform": "lint"}},
    ("b2gtest", "marionette-harness-pytest", "linux64"): {},
    ("b2gtest-emulator", None, "b2g-emu-x86-kk"): {"run": {"machine": {"type": "emulator"}}},

    ("buildbot", None, None): {},
    ("buildbot-try", None, None): {},
    ("buildbot-bridge", None, None): {},
    ("cratertest", None, None): {},

    ("dbg-linux32", "linux32", "linux32"): {"run": {"machine": {"os": "linux32"}}, "build": {"type": ["debug"]}},
    ("dbg-linux64", "linux64", "linux64"): {"run": {"machine": {"os": "linux64"}}, "build": {"type": ["debug"]}},
    ("dbg-macosx64", "macosx64", "osx-10-7"): {"build": {"os": "macosx64"}},


    ("desktop-test", None, "linux64"): {"build": {"platform": "linux64"}},
    ("desktop-test-xlarge", None, "linux64"): {"build": {"platform": "linux64"}},
    ("dolphin", "dolphin-eng", "b2g-device-image"): {},


    ("emulator-ics", "emulator-ics", "b2g-emu-ics"): {"run": {"machine": {"type": "emulator"}}},
    ("emulator-ics-debug", "emulator-ics", "b2g-emu-ics"): {"run": {"machine": {"type": "emulator"}}, "build": {"type": ["debug"]}},
    ("emulator-jb", "emulator-jb", "b2g-emu-jb"): {"run": {"machine": {"type": "emulator"}}},
    ("emulator-jb-debug", "emulator-jb", "b2g-emu-jb"): {"run": {"machine": {"type": "emulator"}}, "build": {"type": ["debug"]}},
    ("emulator-kk", "emulator-kk", "b2g-emu-kk"): {"run": {"machine": {"type": "emulator"}}},
    ("emulator-kk-debug", "emulator-kk", "b2g-emu-kk"): {"run": {"machine": {"type": "emulator"}}},
    ("emulator-l", "emulator-l", "b2g-emu-l"): {"run": {"machine": {"type": "emulator"}}},
    ("emulator-l-debug", "emulator-l", "b2g-emu-l"): {"run": {"machine": {"type": "emulator"}}, "build": {"type": ["debug"]}},
    ("emulator-x86-kk", "emulator-x86-kk", "b2g-emu-x86-kk"): {"run": {"machine": {"type": "emulator"}}},

    ("flame-kk", "aries", "b2g-device-image"): {"run": {"machine": {"type": "aries"}}},
    ("flame-kk", "aries-eng", "b2g-device-image"): {"run": {"machine": {"type": "aries"}}},
    ("flame-kk", "aries-noril", "b2g-device-image"): {"run": {"machine": {"type": "aries"}}},
    ("flame-kk", "flame-kk", "b2g-device-image"): {"run": {"machine": {"type": "flame"}}},
    ("flame-kk", "flame-kk-eng", "b2g-device-image"): {"run": {"machine": {"type": "flame"}}},
    ("flame-kk", "flame-kk-spark-eng", "b2g-device-image"): {"run": {"machine": {"type": "flame"}}},
    ("flame-kk", "nexus-5-user", "b2g-device-image"):{"run": {"machine": {"type": "nexus"}}},

    ("flame-kk", "nexus-4-eng", "b2g-device-image"): {"run": {"machine": {"type": "nexus4"}}},
    ("flame-kk", "nexus-4-kk-eng", "b2g-device-image"): {"run": {"machine": {"type": "nexus4"}}},
    ("flame-kk", "nexus-4-kk-user", "b2g-device-image"): {"run": {"machine": {"type": "nexus4"}}},
    ("flame-kk", "nexus-4-user", "b2g-device-image"): {"run": {"machine": {"type": "nexus4"}}},
    ("flame-kk", "nexus-5-l-eng", "b2g-device-image"): {"run": {"machine": {"type": "nexus5"}}},

    ("funsize-mar-generator", None, "osx-10-10"): {},
    ("funsize-mar-generator", None, "linux64"): {},
    ("funsize-mar-generator", None, "linux32"): {},
    ("funsize-mar-generator", None, "windowsxp"): {},
    ("funsize-mar-generator", None, "windows8-64"): {},
    ("funsize-balrog", None, "osx-10-10"): {},
    ("funsize-balrog", None, "linux32"): {},
    ("funsize-balrog", None, "linux64"): {},
    ("funsize-balrog", None, "windowsxp"):{},
    ("funsize-balrog", None, "windows8-64"):{},
    ("gaia", None, None): {},
    ("gecko-decision", None, None): {},
    ("github-worker", None, None): {},
    ("human-decision", None, None): {},
    ("mulet-opt", "mulet", "mulet-linux64"): {"build": {"platform": "linux64", "type": ["mulet", "opt"]}},
    ("opt-linux32", "linux32", "linux32"): {"run": {"machine": {"os": "linux32"}}, "build": {"platform": "linux32", "type": ["opt"]}},
    ("opt-linux64", "linux64-artifact", "linux64"): {},
    ("opt-linux64", "linux64", "linux64"): {"run": {"machine": {"os": "linux64"}}, "build": {"platform": "linux64", "type": ["opt"]}},
    ("opt-linux64", "linux64-st-an", "linux64"): {"run": {"machine": {"os": "linux64"}}, "build": {"type": ["static analysis", "opt"]}},
    ("opt-macosx64", "macosx64", "osx-10-7"): {"build": {"os": "macosx64"}},
    ("opt-macosx64", "macosx64-st-an", "osx-10-7"): {"build": {"os": "macosx64", "type": ["opt", "static analysis"]}},
    ("signing-worker-v1", None, "linux32"): {},
    ("signing-worker-v1", None, "osx-10-10"):{},
    ("signing-worker-v1", None, "linux64"):{},
    ("signing-worker-v1", None, "windowsxp"): {},
    ("signing-worker-v1", None, "windows8-64"): {},
    ("symbol-upload", None, "linux64"): {},
    ("symbol-upload", None, "android-4-0-armv7-api15"): {},
    ("taskcluster-images", None, "taskcluster-images"):{},
    ("tcvcs-cache-device", None, None): {},



    "android-api-15-frontend": {"run": {"machine": {"os": "android 4.0.3"}}, "build": {"platform": "android"}},
    "emulator-x86-kk": {"run": {"machine": {"type": "emulator"}}, "build": {"platform": "flame"}},
    "eslint-gecko": {"build": {"platform": "lint"}},
    "linux32": {"build": {"platform": "linux32"}},
    "linux64": {"build": {"platform": "linux64"}},
    "linux64-artifact": {},
    "linux64-st-an": {"build": {"platform": "linux64"}},
    "linux64-st-an-debug": {"build": {"platform": "linux64", "type": ["static analysis", "debug"]}},
    "macosx64-st-an": {"build": {"platform": "macosx64", "type": ["static analysis"]}},
    "nexus-5-l-eng": {"build": {"platform": "nexus5"}}
}


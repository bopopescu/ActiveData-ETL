# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (klahnakoski@mozilla.com)

from __future__ import division
from __future__ import unicode_literals

import itertools

from future.utils import text_type

from activedata_etl.transforms.gcov_to_es import ACTIVE_DATA_QUERY
from activedata_etl.transforms.grcov_to_es import download_file
from mo_dots import coalesce
from mo_files import TempFile
from mo_json import stream
from mo_logs import Log
from mo_times import Timer
from pyLibrary.env import http
from pyLibrary.env.big_data import scompressed2ibytes

EXCLUDE = ('mobile',)  # TUPLE OF SOURCE DIRECTORIES TO EXCLUDE
SUITES = {
    "web-platform-tests": {"web-platform", "tests", "test", "wpt"}
}


def make_file_mapper(task_cluster_record):
    # TODO: THERE IS A RISK THE FILE MAPPING MAY CHANGE
    # FIND RECENT FILE LISTING
    timestamp = coalesce(task_cluster_record.repo.push.date, task_cluster_record.repo.changeset.date)
    result = http.post_json(
        ACTIVE_DATA_QUERY,
        json={
            "from": "task.task.artifacts",
            "where": {"and": [
                {"eq": {"name": "public/components.json.gz"}},
                {"eq": {"treeherder.symbol": "Bugzilla"}},
                {"lt": {"repo.push.date": timestamp}}
            ]},
            "sort": {"repo.push.date": "desc"},
            "limit": 1,
            "select": ["url", "repo.push.date"],
            "format": "list"
        }
    )
    return FileMapper(result.data[0].url)


class FileMapper(object):
    """
    MAP FROM COVERAGE FILE RESOURCE NAME TO SOURCE FILENAME
    """

    def __init__(self, files_url):
        """
        :param files_url: EXPECTING URL TO ZIP FILE OF JSON AS ONE OBJECT IN {filename: [product, component]} FORMAT
        """
        self.known_failures = set()
        self.lookup = {}
        with TempFile() as tempfile:
            Log.note("download {{url}}", url=files_url)
            download_file(files_url, tempfile.abspath)
            with open(tempfile.abspath, b"rb") as fstream:
                with Timer("process {{url}}", param={"url": files_url}):
                    for data in stream.parse(
                        scompressed2ibytes(fstream),
                        {"items": "."},
                        {"name"}
                    ):
                        self._add(data.name)

    def _add(self, filename):
        if filename.startswith(EXCLUDE):
            return

        path = list(reversed(filename.split("/")))
        curr = self.lookup
        for i, p in enumerate(path):
            found = curr.get(p)
            if not found:
                curr[p] = filename
                return
            elif isinstance(found, text_type):
                if i + 1 >= len(path):
                    curr[p] = {".": filename}
                else:
                    curr[p] = {path[i + 1]: filename}
                self._add(found)
                return
            else:
                curr = found

    def find(self, filename, suite_name):
        if filename in self.known_failures:
            return filename

        suite_names = SUITES.get(suite_name, {suite_name})

        filename = filename.split(' line ')[0].split(' -> ')[0].split('?')[0].split('#')[0]  # FOR URLS WITH PARAMETERS
        path = list(reversed(filename.split("/")))
        curr = self.lookup
        for i, p in enumerate(path):
            found = curr.get(p)
            if not found:
                if i == 0:  # WE MATCH NOTHING, DO NOT EVEN TRY
                    return filename

                best = self._find_best(path, list(sorted(_values(curr))), suite_names, filename)
                if isinstance(best, text_type):
                    return best
                elif i <= 1:  # IF WE CAN ONLY MATCH THE LOCAL FILENAME,
                    return filename
                else:
                    return best
            elif isinstance(found, text_type):
                return found
            else:
                curr = found

        return self._find_best(path, list(sorted(_values(curr))), suite_names, filename)

    def _find_best(self, path, files, suite_names, default):
        path = set(path)
        best = None
        best_score = 0
        peer = None
        for f in files:
            f_path = set(f.split("/"))
            score = len(path & f_path) + (0.5 * len(suite_names & f_path))
            if score > best_score:
                best = f
                peer = None
                best_score = score
            elif score == best_score:
                peer = f
        if best and not peer:
            return best
        else:
            self.known_failures.add(default)
            return files


def _values(curr):
    for v in curr.values():
        if isinstance(v, text_type):
            yield v
        else:
            for u in _values(v):
                yield u


def _find_best(path, files, default):
    best = None
    best_score = 0
    peer = None
    for f in files:
        f_path = f.split("/")
        score = sum(1 for a, b in itertools.product(path, f_path) if a == b)
        if score > best_score:
            best = f
            peer = None
            best_score = score
        elif score == best_score:
            peer = f
    if best and not peer:
        return best
    else:
        return files

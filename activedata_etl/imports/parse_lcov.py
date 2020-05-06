# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Tyler Blair (tblair@cs.dal.ca)
#

"""
Parses an lcov-generated coverage file and converts it to the JSON format used by other coverage outputs.
"""
from __future__ import division
from __future__ import unicode_literals

import json
import sys

from activedata_etl.imports.coverage_util import LANGUAGE_MAPPINGS
from mo_dots import wrap, Null
from mo_logs import Log

DEBUG = False
DEBUG_LINE_LIMIT = False
EMIT_RECORDS_WITH_ZERO_COVERAGE = True
LINE_LIMIT = 10000

COMMANDS = ('TN:', 'SF:', 'FNF:', 'FNH:', 'LF:', 'LH:', 'LN:', 'DA:', 'FN:', 'FNDA:', 'BRDA:', 'BRF:', 'BRH:', 'end_of_record')


def parse_lcov_coverage(source_key, source_name, stream):
    """
    Parses lcov coverage from a stream
    http://ltp.sourceforge.net/coverage/lcov/geninfo.1.php

    :param source_key:
    :param source_name:
    :param stream:
    :return:
    """

    current_source = None
    done = set()

    for line in stream:
        try:
            if len(line) == 0:
                continue
            elif not line.startswith(COMMANDS):
                source_file += "\n" + line
                continue

            line = line.strip()

            if line == 'end_of_record':
                for source in coco_format(current_source):
                    if source.file.total_covered > LINE_LIMIT:
                        if DEBUG_LINE_LIMIT:
                            Log.warning("{{name}} has {{num}} lines covered", name=source.file.name, num=source.file.total_covered)
                    elif source.file.total_uncovered > LINE_LIMIT:
                        if DEBUG_LINE_LIMIT:
                            Log.warning("{{name}} has {{num}} lines uncovered", name=source.file.name, num=source.file.total_uncovered)
                    elif EMIT_RECORDS_WITH_ZERO_COVERAGE:
                        yield source
                    elif source.file.total_covered:
                        yield source
                current_source = None
            elif ':' in line:
                cmd, data = line.split(":", 1)

                if cmd == 'TN':
                    test_name = data.strip()
                elif cmd == 'SF':
                    source_file = data
                    if source_file in done:
                        Log.error("Note expected to revisit a file")
                    current_source = {
                        'file': source_file,
                        'functions': {},
                        'lines_covered': set(),
                        'lines_uncovered': set()
                    }
                elif cmd == 'FNF':
                    functions_found = int(data)
                elif cmd == 'FNH':
                    functions_hit = int(data)
                elif cmd == 'LF':
                    lines_found = int(data)
                elif cmd == 'LH':
                    lines_hit = int(data)
                elif cmd == 'DA':
                    line_number, execution_count = n_tuple(map(lambda v: int(v) if v else 0, data.split(",")), 2)
                    if execution_count > 0:
                        current_source['lines_covered'].add(line_number)
                    else:
                        current_source['lines_uncovered'].add(line_number)
                elif cmd == 'FN':
                    min_line, function_name = data.split(",", 1)

                    current_source['functions'][function_name] = {
                        'start': int(min_line),
                        'execution_count': 0
                    }
                elif cmd == 'FNDA':
                    try:
                        fn_execution_count, function_name = data.split(",", 1)
                        try:
                            current_source['functions'][function_name]['execution_count'] = int(fn_execution_count)
                        except Exception as e:
                            if fn_execution_count != "0":
                                if DEBUG:
                                    Log.note("No mention of FN:{{func}}, but it has been called", func=function_name, cause=e)
                    except Exception as e:
                        Log.warning("problem with FNDA line {{line|quote}}", line=line, cause=e)
                elif cmd == 'BRDA':
                    # BRDA:<line number>,<block number>,<branch number>,<taken>
                    # <taken> is a number or a dash ("-") or does not exist
                    line, block, branch, taken = n_tuple(data.split(","), 4)
                    pass
                elif cmd == 'BRF':
                    num_branches_found = data
                elif cmd == 'BRH':
                    num_branches_hit = data
                else:
                    Log.error('Unsupported cmd {{cmd}} with data {{data}} in {{source|quote}} for key {{key}}', key=source_key, source=source_name, cmd=cmd, data=data)
            else:
                Log.error("unknown line {{line}} in {{source}}", line=line, source=source_name)
        except Exception as e:
            Log.error("Problem in line {{line}} in {{source}}", line=line, source=source_name, cause=e)


def coco_format(details):
    # TODO: DO NOT IGNORE METHODS
    coverable_line_count = len(details['lines_covered']) + len(details['lines_uncovered'])
    language = [lang for lang, extensions in LANGUAGE_MAPPINGS if details['file'].endswith(extensions)]

    source = wrap({
        "language": language,
        "is_file": True,
        "file": {
            "name": details['file'],
            'covered': sorted(details["lines_covered"]),
            'uncovered': sorted(details['lines_uncovered']),
            "total_covered": len(details['lines_covered']),
            "total_uncovered": len(details['lines_uncovered']),
            "percentage_covered": len(details['lines_covered']) / coverable_line_count if coverable_line_count else None
        }
    })

    return [source]


def n_tuple(values, length):
    """
    RETURN A LIST OF length
    """
    output = [None] * length
    for i, v in enumerate(values):
        if i >= length:
            break
        output[i] = v
    return output


def js_coverage_format(sources):
    results = []
    for key, value in sources.iteritems():
        lines_covered = sorted(value['lines_covered'])

        lines_covered_set = set(lines_covered)

        result = {
            'sourceFile': value['file'],
            'testUrl': value['file'],
            'covered': lines_covered,
            'uncovered': sorted(value['lines_uncovered']),
            'methods': {}
        }

        function_start_lines = sorted([x['start'] for x in value['functions'].values()])

        if len(lines_covered) > 0: # only covered lines are included for methods
            for function_name, function_data in value['functions'].iteritems():
                min_line = function_data['start']
                start_index = function_start_lines.index(min_line)
                max_line = lines_covered[-1] if len(function_start_lines) - 1 == start_index else function_start_lines[start_index + 1]

                function_lines_covered = sorted(lines_covered_set & set(range(min_line, max_line)))

                if len(function_lines_covered) > 0:
                    result['methods'][function_name] = function_lines_covered

        results.append(result)

    return results

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: %s <lcov coverage file>' % sys.argv[0])
        sys.exit(1)

    file_path = sys.argv[1]

    with open(file_path) as f:
        parsed = parse_lcov_coverage(Null, Null, f)

    json.dump(parsed, sys.stdout)

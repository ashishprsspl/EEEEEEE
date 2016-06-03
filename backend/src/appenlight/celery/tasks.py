# -*- coding: utf-8 -*-

# Copyright (C) 2010-2016  RhodeCode GmbH
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License, version 3
# (only), as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# This program is dual-licensed. If you wish to learn more about the
# App Enlight Enterprise Edition, including its added features, Support
# services, and proprietary license terms, please see
# https://rhodecode.com/licenses/

import bisect
import collections
import math
from datetime import datetime, timedelta

import sqlalchemy as sa
import pyelasticsearch

from celery.utils.log import get_task_logger
from zope.sqlalchemy import mark_changed
from pyramid.threadlocal import get_current_request, get_current_registry
from appenlight.celery import celery
from appenlight.models.report_group import ReportGroup
from appenlight.models import DBSession, Datastores
from appenlight.models.report import Report
from appenlight.models.log import Log
from appenlight.models.request_metric import Metric
from appenlight.models.event import Event

from appenlight.models.services.application import ApplicationService
from appenlight.models.services.event import EventService
from appenlight.models.services.log import LogService
from appenlight.models.services.report import ReportService
from appenlight.models.services.report_group import ReportGroupService
from appenlight.models.services.user import UserService
from appenlight.models.tag import Tag
from appenlight.lib import print_traceback
from appenlight.lib.utils import parse_proto, in_batches
from appenlight.lib.ext_json import json
from appenlight.lib.redis_keys import REDIS_KEYS
from appenlight.lib.enums import ReportType

log = get_task_logger(__name__)

sample_boundries = list(range(100, 10000, 100))


def pick_sample(total_occurences, report_type=1):
    every = 1.0
    position = bisect.bisect_left(sample_boundries, total_occurences)
    if position > 0:
        # 404
        if report_type == 2:
            divide = 10.0
        else:
            divide = 100.0
        every = sample_boundries[position - 1] / divide
    return total_occurences % every == 0


@celery.task(queue="default", default_retry_delay=1, max_retries=2)
def test_exception_task():
    log.error('test celery log', extra={'location': 'celery'})
    log.warning('test celery log', extra={'location': 'celery'})
    raise Exception('Celery exception test')


@celery.task(queue="default", default_retry_delay=1, max_retries=2)
def test_retry_exception_task():
    try:
        import time

        time.sleep(1.3)
        log.error('test retry celery log', extra={'location': 'celery'})
        log.warning('test retry celery log', extra={'location': 'celery'})
        raise Exception('Celery exception test')
    except Exception as exc:
        test_retry_exception_task.retry(exc=exc)


@celery.task(queue="reports", default_retry_delay=600, max_retries=999)
def add_reports(resource_id, params, dataset, environ=None, **kwargs):
    proto_version = parse_proto(params.get('protocol_version', ''))
    current_time = datetime.utcnow().replace(second=0, microsecond=0)
    try:
        # we will store solr docs here for single insert
        es_report_docs = {}
        es_report_group_docs = {}
        resource = ApplicationService.by_id(resource_id)
        reports = []

        if proto_version.major < 1 and proto_version.minor < 5:
            for report_data in dataset:
                report_details = report_data.get('report_details', [])
                for i, detail_data in enumerate(report_details):
                    report_data.update(detail_data)
                    report_data.pop('report_details')
                    traceback = report_data.get('traceback')
                    if traceback is None:
                        report_data['traceback'] = report_data.get('frameinfo')
                    # for 0.3 api
                    error = report_data.pop('error_type', '')
                    if error:
                        report_data['error'] = error
                    if proto_version.minor < 4:
                        # convert "Unknown" slow reports to
                        # '' (from older clients)
                        if (report_data['error'] and
                                    report_data['http_status'] < 500):
                            report_data['error'] = ''
                    message = report_data.get('message')
                    if 'extra' not in report_data:
                        report_data['extra'] = []
                    if message:
                        report_data['extra'] = [('message', message), ]
                    reports.append(report_data)
        else:
            reports = dataset

        tags = []
        es_slow_calls_docs = {}
        es_reports_stats_rows = {}
        for report_data in reports:
            # build report details for later
            added_details = 0
            report = Report()
            report.set_data(report_data, resource, proto_version)
            report._skip_ft_index = True

            report_group = ReportGroupService.by_hash_and_resource(
                report.resource_id,
                report.grouping_hash
            )
            occurences = report_data.get('occurences', 1)
            if not report_group:
                # total reports will be +1 moment later
                report_group = ReportGroup(grouping_hash=report.grouping_hash,
                                           occurences=0, total_reports=0,
                                           last_report=0,
                                           priority=report.priority,
                                           error=report.error,
                                           first_timestamp=report.start_time)
                report_group._skip_ft_index = True
                report_group.report_type = report.report_type
            report.report_group_time = report_group.first_timestamp
            add_sample = pick_sample(report_group.occurences,
                                     report_type=report_group.report_type)
            if add_sample:
                resource.report_groups.append(report_group)
                report_group.reports.append(report)
                added_details += 1
                DBSession.flush()
                if report.partition_id not in es_report_docs:
                    es_report_docs[report.partition_id] = []
                es_report_docs[report.partition_id].append(report.es_doc())
                tags.extend(list(report.tags.items()))
                slow_calls = report.add_slow_calls(report_data, report_group)
                DBSession.flush()
                for s_call in slow_calls:
                    if s_call.partition_id not in es_slow_calls_docs:
                        es_slow_calls_docs[s_call.partition_id] = []
                    es_slow_calls_docs[s_call.partition_id].append(
                        s_call.es_doc())
                    # try generating new stat rows if needed
            else:
                # required for postprocessing to not fail later
                report.report_group = report_group

            stat_row = ReportService.generate_stat_rows(
                report, resource, report_group)
            if stat_row.partition_id not in es_reports_stats_rows:
                es_reports_stats_rows[stat_row.partition_id] = []
            es_reports_stats_rows[stat_row.partition_id].append(
                stat_row.es_doc())

            # see if we should mark 10th occurence of report
            last_occurences_10 = int(math.floor(report_group.occurences / 10))
            curr_occurences_10 = int(math.floor(
                (report_group.occurences + report.occurences) / 10))
            last_occurences_100 = int(
                math.floor(report_group.occurences / 100))
            curr_occurences_100 = int(math.floor(
                (report_group.occurences + report.occurences) / 100))
            notify_occurences_10 = last_occurences_10 != curr_occurences_10
            notify_occurences_100 = last_occurences_100 != curr_occurences_100
            report_group.occurences = ReportGroup.occurences + occurences
            report_group.last_timestamp = report.start_time
            report_group.summed_duration = ReportGroup.summed_duration + report.duration
            summed_duration = ReportGroup.summed_duration + report.duration
            summed_occurences = ReportGroup.occurences + occurences
            report_group.average_duration = summed_duration / summed_occurences
            report_group.run_postprocessing(report)
            if added_details:
                report_group.total_reports = ReportGroup.total_reports + 1
                report_group.last_report = report.id
            report_group.set_notification_info(notify_10=notify_occurences_10,
                                               notify_100=notify_occurences_100)
            DBSession.flush()
            report_group.get_report().notify_channel(report_group)
            if report_group.partition_id not in es_report_group_docs:
                es_report_group_docs[report_group.partition_id] = []
            es_report_group_docs[report_group.partition_id].append(
                report_group.es_doc())

            action = 'REPORT'
            log_msg = '%s: %s %s, client: %s, proto: %s' % (
                action,
                report_data.get('http_status', 'unknown'),
                str(resource),
                report_data.get('client'),
                proto_version)
            log.info(log_msg)
        total_reports = len(dataset)
        key = REDIS_KEYS['counters']['reports_per_minute'].format(current_time)
        Datastores.redis.incr(key, total_reports)
        Datastores.redis.expire(key, 3600 * 24)
        key = REDIS_KEYS['counters']['reports_per_minute_per_app'].format(
            resource_id, current_time)
        Datastores.redis.incr(key, total_reports)
        Datastores.redis.expire(key, 3600 * 24)

        add_reports_es(es_report_group_docs, es_report_docs)
        add_reports_slow_calls_es(es_slow_calls_docs)
        add_reports_stats_rows_es(es_reports_stats_rows)
        return True
    except Exception as exc:
        print_traceback(log)
        add_reports.retry(exc=exc)


@celery.task(queue="es", default_retry_delay=600, max_retries=999)
def add_reports_es(report_group_docs, report_docs):
    for k, v in report_group_docs.items():
        Datastores.es.bulk_index(k, 'report_group', v, id_field="_id")
    for k, v in report_docs.items():
        Datastores.es.bulk_index(k, 'report', v, id_field="_id",
                                 parent_field='_parent')


@celery.task(queue="es", default_retry_delay=600, max_retries=999)
def add_reports_slow_calls_es(es_docs):
    for k, v in es_docs.items():
        Datastores.es.bulk_index(k, 'log', v)


@celery.task(queue="es", default_retry_delay=600, max_retries=999)
def add_reports_stats_rows_es(es_docs):
    for k, v in es_docs.items():
        Datastores.es.bulk_index(k, 'log', v)


@celery.task(queue="logs", default_retry_delay=600, max_retries=999)
def add_logs(resource_id, request, dataset, environ=None, **kwargs):
    proto_version = request.get('protocol_version')
    current_time = datetime.utcnow().replace(second=0, microsecond=0)

    try:
        es_docs = collections.defaultdict(list)
        application = ApplicationService.by_id(resource_id)
        ns_pairs = []
        for entry in dataset:
            # gather pk and ns so we can remove older versions of row later
            if entry['primary_key'] is not None:
                ns_pairs.append({"pk": entry['primary_key'],
                                 "ns": entry['namespace']})
            log_entry = Log()
            log_entry.set_data(entry, resource=application)
            log_entry._skip_ft_index = True
            application.logs.append(log_entry)
            DBSession.flush()
            # insert non pk rows first
            if entry['primary_key'] is None:
                es_docs[log_entry.partition_id].append(log_entry.es_doc())

        # 2nd pass to delete all log entries from db foe same pk/ns pair
        if ns_pairs:
            ids_to_delete = []
            es_docs = collections.defaultdict(list)
            es_docs_to_delete = collections.defaultdict(list)
            found_pkey_logs = LogService.query_by_primary_key_and_namespace(
                list_of_pairs=ns_pairs)
            log_dict = {}
            for log_entry in found_pkey_logs:
                log_key = (log_entry.primary_key, log_entry.namespace)
                if log_key not in log_dict:
                    log_dict[log_key] = []
                log_dict[log_key].append(log_entry)

            for ns, entry_list in log_dict.items():
                entry_list = sorted(entry_list, key=lambda x: x.timestamp)
                # newest row needs to be indexed in es
                log_entry = entry_list[-1]
                # delete everything from pg and ES, leave the last row in pg
                for e in entry_list[:-1]:
                    ids_to_delete.append(e.log_id)
                    es_docs_to_delete[e.partition_id].append(e.delete_hash)

                es_docs_to_delete[log_entry.partition_id].append(
                    log_entry.delete_hash)

                es_docs[log_entry.partition_id].append(log_entry.es_doc())

            if ids_to_delete:
                query = DBSession.query(Log).filter(
                    Log.log_id.in_(ids_to_delete))
                query.delete(synchronize_session=False)
            if es_docs_to_delete:
                # batch this to avoid problems with default ES bulk limits
                for es_index in es_docs_to_delete.keys():
                    for batch in in_batches(es_docs_to_delete[es_index], 20):
                        query = {'terms': {'delete_hash': batch}}

                        try:
                            Datastores.es.delete_by_query(
                                es_index, 'log', query)
                        except pyelasticsearch.ElasticHttpNotFoundError as exc:
                            log.error(exc)

        total_logs = len(dataset)

        log_msg = 'LOG_NEW: %s, entries: %s, proto:%s' % (
            str(application),
            total_logs,
            proto_version)
        log.info(log_msg)
        # mark_changed(session)
        key = REDIS_KEYS['counters']['logs_per_minute'].format(current_time)
        Datastores.redis.incr(key, total_logs)
        Datastores.redis.expire(key, 3600 * 24)
        key = REDIS_KEYS['counters']['logs_per_minute_per_app'].format(
            resource_id, current_time)
        Datastores.redis.incr(key, total_logs)
        Datastores.redis.expire(key, 3600 * 24)
        add_logs_es(es_docs)
        return True
    except Exception as exc:
        print_traceback(log)
        add_logs.retry(exc=exc)


@celery.task(queue="es", default_retry_delay=600, max_retries=999)
def add_logs_es(es_docs):
    for k, v in es_docs.items():
        Datastores.es.bulk_index(k, 'log', v)


@celery.task(queue="metrics", default_retry_delay=600, max_retries=999)
def add_metrics(resource_id, request, dataset, proto_version):
    current_time = datetime.utcnow().replace(second=0, microsecond=0)
    try:
        application = ApplicationService.by_id_cached()(resource_id)
        application = DBSession.merge(application, load=False)
        es_docs = []
        rows = []
        for metric in dataset:
            tags = dict(metric['tags'])
            server_n = tags.get('server_name', metric['server_name']).lower()
            tags['server_name'] = server_n or 'unknown'
            new_metric = Metric(
                timestamp=metric['timestamp'],
                resource_id=application.resource_id,
                namespace=metric['namespace'],
                tags=tags)
            rows.append(new_metric)
            es_docs.append(new_metric.es_doc())
        session = DBSession()
        session.bulk_save_objects(rows)
        session.flush()

        action = 'METRICS'
        metrics_msg = '%s: %s, metrics: %s, proto:%s' % (
            action,
            str(application),
            len(dataset),
            proto_version
        )
        log.info(metrics_msg)

        mark_changed(session)
        key = REDIS_KEYS['counters']['metrics_per_minute'].format(current_time)
        Datastores.redis.incr(key, len(rows))
        Datastores.redis.expire(key, 3600 * 24)
        key = REDIS_KEYS['counters']['metrics_per_minute_per_app'].format(
            resource_id, current_time)
        Datastores.redis.incr(key, len(rows))
        Datastores.redis.expire(key, 3600 * 24)
        add_metrics_es(es_docs)
        return True
    except Exception as exc:
        print_traceback(log)
        add_metrics.retry(exc=exc)


@celery.task(queue="es", default_retry_delay=600, max_retries=999)
def add_metrics_es(es_docs):
    for doc in es_docs:
        partition = 'rcae_m_%s' % doc['timestamp'].strftime('%Y_%m_%d')
        Datastores.es.index(partition, 'log', doc)


@celery.task(queue="default", default_retry_delay=5, max_retries=2)
def check_user_report_notifications(resource_id, since_when=None):
    try:
        request = get_current_request()
        application = ApplicationService.by_id(resource_id)
        if not application:
            return
        error_key = REDIS_KEYS['reports_to_notify_per_type_per_app'].format(
            ReportType.error, resource_id)
        slow_key = REDIS_KEYS['reports_to_notify_per_type_per_app'].format(
            ReportType.slow, resource_id)
        error_group_ids = Datastores.redis.smembers(error_key)
        slow_group_ids = Datastores.redis.smembers(slow_key)
        Datastores.redis.delete(error_key)
        Datastores.redis.delete(slow_key)
        err_gids = [int(g_id) for g_id in error_group_ids]
        slow_gids = [int(g_id) for g_id in list(slow_group_ids)]
        group_ids = err_gids + slow_gids
        occurence_dict = {}
        for g_id in group_ids:
            key = REDIS_KEYS['counters']['report_group_occurences'].format(
                g_id)
            val = Datastores.redis.get(key)
            Datastores.redis.delete(key)
            if val:
                occurence_dict[g_id] = int(val)
            else:
                occurence_dict[g_id] = 1
        report_groups = ReportGroupService.by_ids(group_ids)
        report_groups.options(sa.orm.joinedload(ReportGroup.last_report_ref))

        ApplicationService.check_for_groups_alert(
            application, 'alert', report_groups=report_groups,
            occurence_dict=occurence_dict, since_when=since_when)
        users = set([p.user for p in application.users_for_perm('view')])
        report_groups = report_groups.all()
        for user in users:
            UserService.report_notify(user, request, application,
                                      report_groups=report_groups,
                                      occurence_dict=occurence_dict,
                                      since_when=since_when)
        for group in report_groups:
            # marks report_groups as notified
            if not group.notified:
                group.notified = True
    except Exception as exc:
        print_traceback(log)
        raise


@celery.task(queue="default", default_retry_delay=1, max_retries=2)
def close_alerts(since_when=None):
    log.warning('Checking alerts')
    try:
        event_types = [Event.types['error_report_alert'],
                       Event.types['slow_report_alert'], ]
        statuses = [Event.statuses['active']]
        # get events older than 5 min
        events = EventService.by_type_and_status(
            event_types,
            statuses,
            older_than=(since_when - timedelta(minutes=5)))
        for event in events:
            # see if we can close them
            event.validate_or_close(
                since_when=(since_when - timedelta(minutes=1)))
    except Exception as exc:
        print_traceback(log)
        raise


@celery.task(queue="default", default_retry_delay=600, max_retries=999)
def update_tag_counter(tag_name, tag_value, count):
    try:
        query = DBSession.query(Tag).filter(Tag.name == tag_name).filter(
            sa.cast(Tag.value, sa.types.TEXT) == sa.cast(json.dumps(tag_value),
                                                         sa.types.TEXT))
        query.update({'times_seen': Tag.times_seen + count,
                      'last_timestamp': datetime.utcnow()},
                     synchronize_session=False)
        session = DBSession()
        mark_changed(session)
        return True
    except Exception as exc:
        print_traceback(log)
        update_tag_counter.retry(exc=exc)


@celery.task(queue="default")
def update_tag_counters():
    """
    Sets task to update counters for application tags
    """
    tags = Datastores.redis.lrange(REDIS_KEYS['seen_tag_list'], 0, -1)
    Datastores.redis.delete(REDIS_KEYS['seen_tag_list'])
    c = collections.Counter(tags)
    for t_json, count in c.items():
        tag_info = json.loads(t_json)
        update_tag_counter.delay(tag_info[0], tag_info[1], count)


@celery.task(queue="default")
def daily_digest():
    """
    Sends daily digest with top 50 error reports
    """
    request = get_current_request()
    apps = Datastores.redis.smembers(REDIS_KEYS['apps_that_had_reports'])
    Datastores.redis.delete(REDIS_KEYS['apps_that_had_reports'])
    since_when = datetime.utcnow() - timedelta(hours=8)
    log.warning('Generating daily digests')
    for resource_id in apps:
        resource_id = resource_id.decode('utf8')
        end_date = datetime.utcnow().replace(microsecond=0, second=0)
        filter_settings = {'resource': [resource_id],
                           'tags': [{'name': 'type',
                                     'value': ['error'], 'op': None}],
                           'type': 'error', 'start_date': since_when,
                           'end_date': end_date}

        reports = ReportGroupService.get_trending(
            request, filter_settings=filter_settings, limit=50)

        application = ApplicationService.by_id(resource_id)
        if application:
            users = set([p.user for p in application.users_for_perm('view')])
            for user in users:
                user.send_digest(request, application, reports=reports,
                                 since_when=since_when)


@celery.task(queue="default")
def alerting():
    """
    Loop that checks redis for info and then issues new tasks to celery to
    perform the following:
    - which applications should have new alerts opened
    - which currently opened alerts should be closed
    """
    start_time = datetime.utcnow()
    # transactions are needed for mailer
    apps = Datastores.redis.smembers(REDIS_KEYS['apps_that_had_reports'])
    Datastores.redis.delete(REDIS_KEYS['apps_that_had_reports'])
    for app in apps:
        log.warning('Notify for app: %s' % app)
        check_user_report_notifications.delay(app.decode('utf8'))
    # clear app ids from set
    close_alerts.delay(since_when=start_time)


@celery.task(queue="default", soft_time_limit=3600 * 4, hard_time_limit=3600 * 4,
      max_retries=999)
def logs_cleanup(resource_id, filter_settings):
    request = get_current_request()
    request.tm.begin()
    es_query = {
        "_source": False,
        "size": 5000,
        "query": {
            "filtered": {
                "filter": {
                    "and": [{"term": {"resource_id": resource_id}}]
                }
            }
        }
    }

    query = DBSession.query(Log).filter(Log.resource_id == resource_id)
    if filter_settings['namespace']:
        query = query.filter(Log.namespace == filter_settings['namespace'][0])
        es_query['query']['filtered']['filter']['and'].append(
            {"term": {"namespace": filter_settings['namespace'][0]}}
        )
    query.delete(synchronize_session=False)
    request.tm.commit()
    result = request.es_conn.search(es_query, index='rcae_l_*',
                                    doc_type='log', es_scroll='1m',
                                    es_search_type='scan')
    scroll_id = result['_scroll_id']
    while True:
        log.warning('log_cleanup, app:{} ns:{} batch'.format(
            resource_id,
            filter_settings['namespace']
        ))
        es_docs_to_delete = []
        result = request.es_conn.send_request(
            'POST', ['_search', 'scroll'],
            body=scroll_id, query_params={"scroll": '1m'})
        scroll_id = result['_scroll_id']
        if not result['hits']['hits']:
            break
        for doc in result['hits']['hits']:
            es_docs_to_delete.append({"id": doc['_id'],
                                      "index": doc['_index']})

        for batch in in_batches(es_docs_to_delete, 10):
            Datastores.es.bulk([Datastores.es.delete_op(doc_type='log',
                                                        **to_del)
                                for to_del in batch])
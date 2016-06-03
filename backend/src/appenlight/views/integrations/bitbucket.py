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

from appenlight.models.integrations.bitbucket import BitbucketIntegration, \
    IntegrationException
from appenlight.models.report_comment import ReportComment
from appenlight.models.services.report_group import ReportGroupService
from pyramid.view import view_config
from appenlight import forms
import logging
from datetime import datetime
from pyramid.httpexceptions import HTTPUnprocessableEntity
from webob.multidict import MultiDict

log = logging.getLogger(__name__)

from . import IntegrationView


class BitbucketView(IntegrationView):
    @view_config(route_name='integrations_id',
                 match_param=['action=info', 'integration=bitbucket'],
                 renderer='json')
    def get_bitbucket_info(self):
        """
        Grab information about possible priority levels and assignable users
        """
        try:
            client = BitbucketIntegration.create_client(
                self.request,
                self.integration.config['user_name'],
                self.integration.config['repo_name'])
        except IntegrationException as e:
            self.request.response.status_code = 503
            return {'error_messages': [str(e)]}
        assignees = client.get_assignees()
        priorities = client.get_priorities()
        return {'assignees': assignees,
                'priorities': priorities}

    @view_config(route_name='integrations_id',
                 match_param=['action=create-issue',
                              'integration=bitbucket'],
                 renderer='json')
    def create_issue(self):
        """
        Creates a new issue in bitbucket issue tracker from report group
        """
        report = ReportGroupService.by_id(
            self.request.unsafe_json_body['group_id'])
        form_data = {
            'title': self.request.unsafe_json_body.get('title',
                                                       'Unknown Title'),
            'content': self.request.unsafe_json_body.get('content', ''),
            'kind': 'bug',
            'priority': self.request.unsafe_json_body['priority'],
            'responsible': self.request.unsafe_json_body['responsible']['user']
        }

        try:
            client = BitbucketIntegration.create_client(
                self.request,
                self.integration.config['user_name'],
                self.integration.config['repo_name'])
            issue = client.create_issue(form_data)
        except IntegrationException as e:
            self.request.response.status_code = 503
            return {'error_messages': [str(e)]}

        comment_body = 'Bitbucket issue created: %s ' % issue['web_url']
        comment = ReportComment(user_name=self.request.user.user_name,
                                report_time=report.first_timestamp,
                                body=comment_body)
        report.comments.append(comment)
        return True

    @view_config(route_name='integrations_id',
                 match_param=['action=setup', 'integration=bitbucket'],
                 renderer='json',
                 permission='edit')
    def setup(self):
        """
        Validates and creates integration between application and bitbucket
        """
        resource = self.request.context.resource
        form = forms.IntegrationBitbucketForm(
            MultiDict(self.request.safe_json_body or {}),
            csrf_context=self.request, **self.integration_config)
        if self.request.method == 'POST' and form.validate():
            integration_config = {
                'repo_name': form.repo_name.data,
                'user_name': form.user_name.data,
                'host_name': 'https://bitbucket.org'
            }
            if not self.integration:
                # add new integration
                self.integration = BitbucketIntegration(
                    modified_date=datetime.utcnow(),
                )
                self.request.session.flash('Integration added')
                resource.integrations.append(self.integration)
            else:
                self.request.session.flash('Integration updated')
            self.integration.config = integration_config
            return integration_config
        elif self.request.method == 'POST':
            return HTTPUnprocessableEntity(body=form.errors_json)
        return self.integration_config
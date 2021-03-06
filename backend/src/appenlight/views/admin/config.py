# -*- coding: utf-8 -*-

# Copyright 2010 - 2017 RhodeCode GmbH and the AppEnlight project authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pyramid.view import view_config
from pyramid.httpexceptions import HTTPNotFound
from appenlight.models.services.config import ConfigService

import logging

log = logging.getLogger(__name__)


@view_config(route_name='admin_configs', renderer='json',
             permission='root_administration', request_method='GET')
def query(request):
    ConfigService.setup_default_values()
    pairs = []
    for value in request.GET.getall('filter'):
        split = value.split(':', 1)
        pairs.append({'key': split[0], 'section': split[1]})
    return [c for c in ConfigService.filtered_key_and_section(pairs)]


@view_config(route_name='admin_config', renderer='json',
             permission='root_administration', request_method='POST')
def post(request):
    row = ConfigService.by_key_and_section(
        key=request.matchdict.get('key'),
        section=request.matchdict.get('section'))
    if not row:
        raise HTTPNotFound()
    row.value = None
    row.value = request.unsafe_json_body['value']
    return row

# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class Meeting(models.Model):
    _inherit = 'calendar.event'

    editable = fields.Boolean(compute='_compute_even_is_editable')

    @api.multi
    def _compute_even_is_editable(self):
        for event in self:
            activity = self.env['mail.activity'].search([('calendar_event_id', '=', event.id)])
            event.editable = False if activity.done else True

    @api.model
    def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None):
        """
        Performs a ``search()`` followed by a ``read()``.

        :param domain: Search domain, see ``args`` parameter in ``search()``. Defaults to an empty domain that will match all records.
        :param fields: List of fields to read, see ``fields`` parameter in ``read()``. Defaults to all fields.
        :param offset: Number of records to skip, see ``offset`` parameter in ``search()``. Defaults to 0.
        :param limit: Maximum number of records to return, see ``limit`` parameter in ``search()``. Defaults to no limit.
        :param order: Columns to sort result, see ``order`` parameter in ``search()``. Defaults to no sort.
        :return: List of dictionaries containing the asked fields.
        :rtype: List of dictionaries.

        """
        records = self.search(domain or [], offset=offset, limit=limit, order=order)
        if not records:
            return []

        if fields and fields == ['id']:
            # shortcut read if we only want the ids
            return [{'id': record.id} for record in records]

        # read() ignores active_test, but it would forward it to any downstream search call
        # (e.g. for x2m or function fields), and this is not the desired behavior, the flag
        # was presumably only meant for the main search().
        # TODO: Move this to read() directly?
        if 'active_test' in self._context:
            context = dict(self._context)
            del context['active_test']
            records = records.with_context(context)

        fields.append('editable')
        result = records.read(fields)
        if len(result) <= 1:
            return result

        # reorder read
        index = {vals['id']: vals for vals in result}
        return [index[record.id] for record in records if record.id in index]

    @api.model
    def create(self, values):

        if not 'user_id' in values:  # Else bug with quick_create when we are filter on an other user
            values['user_id'] = self.env.user.id
        if 'res_id' in values and values['res_id'] == 0:
            del values['res_id']

        # compute duration, if not given
        if not 'duration' in values:
            values['duration'] = self._get_duration(values['start'], values['stop'])

        # created from calendar: try to create an activity on the related record
        if not values.get('activity_ids'):
            defaults = self.default_get(['activity_ids', 'res_model_id', 'res_id', 'user_id'])
            res_model_id = values.get('res_model_id', defaults.get('res_model_id'))
            res_id = values.get('res_id', defaults.get('res_id'))

            user_id = values.get('user_id', defaults.get('user_id'))
            if not defaults.get('activity_ids'):
                if res_model_id:
                    if res_id:
                        has_attr = hasattr(self.env[self.env['ir.model'].sudo().browse(res_model_id).model], 'activity_ids')
                        if has_attr:
                            meeting_activity_type = self.env['mail.activity.type'].search([('category', '=', 'meeting')],
                                                                                          limit=1)
                            if meeting_activity_type:
                                activity_vals = {
                                    'res_model_id': res_model_id,
                                    'res_id': res_id,
                                    'activity_type_id': meeting_activity_type.id,
                                }
                                if user_id:
                                    activity_vals['user_id'] = user_id
                                values['activity_ids'] = [(0, 0, activity_vals)]

        meeting = super(Meeting, self).create(values)
        meeting._sync_activities(values)

        final_date = meeting._get_recurrency_end_date()
        # `dont_notify=True` in context to prevent multiple notify_next_alarm
        meeting.with_context(dont_notify=True).write({'final_date': final_date})
        meeting.with_context(dont_notify=True).create_attendees()

        # Notify attendees if there is an alarm on the created event, as it might have changed their
        # next event notification
        if not self._context.get('dont_notify'):
            if len(meeting.alarm_ids) > 0:
                self.env['calendar.alarm_manager'].notify_next_alarm(meeting.partner_ids.ids)
        return meeting

    @api.model
    def default_get(self, fields):
        # super default_model='crm.lead' for easier use in adddons
        if self.env.context.get('default_res_model') and not self.env.context.get('default_res_model_id'):
            self = self.with_context(
                default_res_model_id=self.env['ir.model'].sudo().search([
                    ('model', '=', self.env.context['default_res_model'])
                ], limit=1).id
            )

        defaults = super(Meeting, self).default_get(fields)

        # support active_model / active_id as replacement of default_* if not already given
        if 'res_model_id' not in defaults and 'res_model_id' in fields and \
                self.env.context.get('active_model') and self.env.context['active_model'] != 'calendar.event':
            defaults['res_model_id'] = self.env['ir.model'].sudo().search([('model', '=', self.env.context['active_model'])], limit=1).id
        if 'res_id' not in defaults and 'res_id' in fields and \
                defaults.get('res_model_id') and self.env.context.get('active_id'):
            defaults['res_id'] = self.env.context['active_id']

        return defaults

    def _sync_activities(self, values):
        # update activities
        if self.mapped('activity_ids'):
            activity_values = {}
            if values.get('name'):
                activity_values['summary'] = values['name']
            if values.get('description'):
                activity_values['note'] = values['description']
            if values.get('start'):
                activity_values['date_deadline'] = fields.Datetime.from_string(values['start']).date()
            if values.get('user_id'):
                activity_values['user_id'] = values['user_id']
            if values.get('done'):
                activity_values['done'] = values['done']
            else:
                activity_values['done'] = False
            if activity_values.keys():
                self.mapped('activity_ids').write(activity_values)

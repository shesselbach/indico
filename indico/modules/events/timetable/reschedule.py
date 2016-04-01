# This file is part of Indico.
# Copyright (C) 2002 - 2016 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from datetime import timedelta, datetime

from pytz import utc
from werkzeug.utils import cached_property

from indico.core.db import db
from indico.core.errors import UserValueError
from indico.modules.events.timetable.models.entries import TimetableEntry, TimetableEntryType
from indico.util.i18n import _
from indico.util.struct.enum import IndicoEnum
from indico.util.struct.iterables import materialize_iterable, window


class RescheduleMode(unicode, IndicoEnum):
    none = 'none'  # no action, just fit blocks..
    time = 'time'
    duration = 'duration'


class Reschedule(object):
    """
    Compacts the the schedule of an event day by either adjusting
    start times or durations of timetable entries.

    :param event: The event of which the timetable entries should
                  be rescheduled.
    :param mode: A `RescheduleMode` value specifying whether the
                 duration or start time should be adjusted.
    :param day: A `date` specifying the day to reschedule (the day of
                the timetable entries are determined using the event's
                timezone)
    :param session: If specified, only blocks of that session will be
                    rescheduled, ignoring any other timetable entries.
                    Cannot be combined with `session_block`.
    :param session_block`: If specified, only entries inside that
                           block will be rescheduled.  Cannot be
                           combined with `session`.
    :param fit_blocks: Whether session blocks should be resized to
                       exactly fit their contents before the actual
                       rescheduling operation.
    :param gap: A timedelta specifying the cap between rescheduled
                timetable entries.
    """

    def __init__(self, event, mode, day, session=None, session_block=None, fit_blocks=False, gap=timedelta()):
        assert session is None or session_block is None, \
            'session and session_block are mutually exclusive'
        self.event = event
        self.mode = mode
        self.day = day
        self.session = session
        self.session_block = session_block
        self.fit_blocks = fit_blocks
        self.gap = gap

    def run(self):
        """Perform the rescheduling"""
        if self.fit_blocks:
            self._fit_blocks()
        if self.mode == RescheduleMode.time:
            self._reschedule_time()
        elif self.mode == RescheduleMode.duration:
            self._reschedule_duration()
        db.session.flush()

    def _reschedule_time(self):
        start_dt = self._start_dt
        for entry in self._entries:
            entry.move(start_dt)
            start_dt = entry.end_dt + self.gap

    def _reschedule_duration(self):
        for entry, successor in window(self._entries):
            duration = successor.start_dt - entry.start_dt - self.gap
            if duration <= timedelta(0):
                raise UserValueError(_("The chosen time gap would result in an entry with a duration of less than a "
                                       "minute. Please choose a smaller gap between entries."))
            entry.object.duration = duration

    def _fit_blocks(self):
        for entry in self._entries:
            if entry.type == TimetableEntryType.SESSION_BLOCK:
                self._fit_block(entry)

    def _fit_block(self, entry):
        children = entry.children
        if not children:
            return
        entry.start_dt = children[0].start_dt
        entry.session_block.duration = children[-1].end_dt - entry.start_dt

    @cached_property
    def _start_dt(self):
        if self.session_block:
            return self.session_block.timetable_entry.start_dt
        else:
            time = self.event.start_dt_local.time()
            dt = datetime.combine(self.day, time)
            return self.event.tzinfo.localize(dt).astimezone(utc)

    @cached_property
    @materialize_iterable()
    def _entries(self):
        if self.session_block:
            # if we have a session block we reschedule the entries inside that block
            for entry in self.session_block.timetable_entry.children:
                # the block should only have entries on the same day
                assert entry.start_dt.astimezone(self.event.tzinfo).date() == self.day
                yield entry
        elif self.session:
            # if we have a session we reschedule the blocks of that session on the day
            for block in self.session.blocks:
                if not block.timetable_entry:
                    continue
                if block.timetable_entry.start_dt.astimezone(self.event.tzinfo).date() == self.day:
                    yield block.timetable_entry
        else:
            # if we are on the top level we reschedule all top-level entries on the day
            query = (self.event.timetable_entries
                     .filter(TimetableEntry.parent_id.is_(None),
                             db.cast(TimetableEntry.start_dt.astimezone(self.event.tzinfo), db.Date) == self.day))
            for entry in query:
                yield entry
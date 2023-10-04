"""
Heating Prediction app
see Readme for information
"""
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta
import math
import re
import time
import pytz
import appdaemon.plugins.hass.hassapi as hass
import requests
import copy

THIS_VERSION = 'v0.1'
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
MAX_INCREMENT = 100

CONFIG_ITEMS = [
    {'name' : 'version',   'friendly_name' : 'Predheat Core Update',  'type' : 'update', 'title' : 'Predheat', 'installed_version' : THIS_VERSION, 'release_url' : 'https://github.com/springfall2008/predheat/releases/tag/' + THIS_VERSION, 'entity_picture' : 'https://user-images.githubusercontent.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png'},
    {'name' : 'test',      'friendly_name' : 'test', 'type' : 'switch'},
]

class PredHeat(hass.Hass):
    """ 
    The heating prediction class itself 
    """
    def minutes_to_time(self, updated, now):
        """
        Compute the number of minutes between a time (now) and the updated time
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60*24)
        return minutes

    def str2time(self, str):
        if '.' in str:
            tdata = datetime.strptime(str, TIME_FORMAT_SECONDS)
        else:
            tdata = datetime.strptime(str, TIME_FORMAT)
        return tdata

    def call_notify(self, message):
        """
        Send HA notifications
        """
        for device in self.notify_devices:
            self.call_service("notify/" + device, message=message)

    def resolve_arg(self, arg, value, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Resolve argument templates and state instances
        """
        if isinstance(value, list) and (index is not None):
            if index < len(value):
                value = value[index]
            else:
                self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))
                value = None
            index = None

        if index:
            self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))

        # If we have a list of items get each and add them up or return them as a list
        if isinstance(value, list):
            if combine:
                final = 0
                for item in value:
                    got = self.resolve_arg(arg, item, default=default, indirect=True)
                    try:
                        final += float(got)
                    except (ValueError, TypeError):
                        self.log("WARN: Return bad value {} from {} arg {}".format(got, item, arg))
                        self.record_status("Warn - Return bad value {} from {} arg {}".format(got, item, arg), had_errors=True) 
                return final
            else:
                final = []
                for item in value:
                    item = self.resolve_arg(arg, item, default=default, indirect=indirect)
                    final.append(item)
                return final

        # Resolve templated data
        for repeat in range(0, 2):
            if isinstance(value, str) and '{' in value:
                try:
                    value = value.format(**self.args)
                except KeyError:
                    self.log("WARN: can not resolve {} value {}".format(arg, value))
                    self.record_status("Warn - can not resolve {} value {}".format(arg, value), had_errors=True)
                    value = default

        # Resolve indirect instance
        if indirect and isinstance(value, str) and '.' in value:
            ovalue = value
            if attribute:
                value = self.get_state(entity_id = value, default=default, attribute=attribute)
            else:
                value = self.get_state(entity_id = value, default=default)
        return value

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Argument getter that can use HA state as well as fixed values
        """
        value = None

        # Get From HA config
        value = self.get_ha_config(arg)

        # Resolve locally if no HA config
        if value is None:
            value = self.args.get(arg, default)
            value = self.resolve_arg(arg, value, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index)

        if isinstance(default, float):
            # Convert to float?
            try:
                value = float(value)
            except (ValueError, TypeError):
                self.log("WARN: Return bad float value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn - Return bad float value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, int) and not isinstance(default, bool):
            # Convert to int? 
            try:
                value = int(float(value))
            except (ValueError, TypeError):
                self.log("WARN: Return bad int value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn - Return bad int value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, bool) and isinstance(value, str):
            # Convert to Boolean
            if value.lower() in ['on', 'true', 'yes', 'enabled', 'enable', 'connected']:
                value = True
            else:
                value = False
        elif isinstance(default, list):
            # Convert to list?
            if not isinstance(value, list):
                value = [value]
                
        # Set to user config
        self.expose_config(arg, value)
        return value    

    def reset(self):
        self.had_errors = False
        self.prediction_started = False
        self.update_pending = False
        self.prefix = self.args.get('prefix', "predheat")
        self.max_days_previous = 1

    def record_status(self, message, debug="", had_errors = False):
        """
        Records status to HA sensor
        """
        self.set_state(self.prefix + ".status", state=message, attributes = {'friendly_name' : 'Status', 'icon' : 'mdi:information', 'last_updated' : datetime.now(), 'debug' : debug})
        if had_errors:
            self.had_errors = True

    def minute_data(self, history, days, now, state_key, last_updated_key,
                    backwards=False, to_key=None, smoothing=False, clean_increment=False, divide_by=0, scale=1.0, accumulate=[], adjust_key=None):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        adata = {}
        newest_state = 0
        last_state = 0
        newest_age = 999999
        prev_last_updated_time = None
        max_increment = MAX_INCREMENT

        # Check history is valid
        if not history:
            self.log("Warning, empty history passed to minute_data, ignoring (check your settings)...")
            return mdata

        # Process history
        for item in history:

            # Ignore data without correct keys
            if state_key not in item:
                continue
            if last_updated_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == 'unavailable' or item[state_key] == 'unknown':
                continue

            # Get the numerical key and the timestamp and ignore if in error
            try:
                state = float(item[state_key]) * scale
                last_updated_time = self.str2time(item[last_updated_key])
            except (ValueError, TypeError):
                continue

            # Divide down the state if required
            if divide_by:
                state /= divide_by
            
            # Update prev to the first if not set
            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

            # Intelligent adjusted?
            if adjust_key:
                adjusted = item.get(adjust_key, False)
            else:
                adjusted = False

            # Work out end of time period
            # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
            if to_key:
                to_value = item[to_key]
                if not to_value:
                    to_time = now + timedelta(minutes=24*60*self.forecast_days)
                else:
                    to_time = self.str2time(item[to_key])
            else:
                if backwards:
                    to_time = prev_last_updated_time
                else:
                    to_time = None

            if backwards:
                timed = now - last_updated_time
                if to_time:
                    timed_to = now - to_time
            else:
                timed = last_updated_time - now
                if to_time:
                    timed_to = to_time - now

            minutes = int(timed.seconds / 60) + int(timed.days * 60*24)
            if to_time:
                minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60*24)

            if minutes < newest_age:
                newest_age = minutes
                newest_state = state

            if to_time:
                minute = minutes
                if minute == minutes_to:
                    mdata[minute] = state
                else:
                    if smoothing:
                        # Reset to zero, sometimes not exactly zero
                        if state < last_state and (state <= (last_state / 10.0)):
                            while minute < minutes_to:
                                mdata[minute] = state
                                minute += 1
                        else:
                            # Can't really go backwards as incrementing data
                            if state < last_state:
                                state = last_state

                            # Create linear function
                            diff = (state - last_state) / (minutes_to - minute)

                            # If the spike is too big don't smooth it, it will removed in the clean function later
                            if max_increment > 0 and diff > max_increment:
                                diff = 0

                            index = 0
                            while minute < minutes_to:
                                mdata[minute] = state - diff*index
                                minute += 1
                                index += 1
                    else:
                        while minute < minutes_to:
                            mdata[minute] = state
                            if adjusted:
                                adata[minute] = True
                            minute += 1
            else:
                mdata[minutes] = state

            # Store previous time & state
            prev_last_updated_time = last_updated_time
            last_state = state

        # If we only have a start time then fill the gaps with the last values
        if not to_key:
            state = newest_state
            for minute in range(0, 60*24*days):
                rindex = 60*24*days - minute - 1
                state = mdata.get(rindex, state)
                mdata[rindex] = state
                minute += 1

        # Reverse data with smoothing 
        if clean_increment:
            mdata = self.clean_incrementing_reverse(mdata, max_increment)

        # Accumulate to previous data?
        if accumulate:
            for minute in range(0, 60*24*days):
                if minute in mdata:
                    mdata[minute] += accumulate.get(minute, 0)
                else:
                    mdata[minute] = accumulate.get(minute, 0)

        if adjust_key:
            self.io_adjusted = adata
        return mdata

    def clean_incrementing_reverse(self, data, max_increment=0):
        """
        Cleanup an incrementing sensor data that runs backwards in time to remove the
        resets (where it goes back to 0) and make it always increment
        """
        new_data = {}
        length = max(data) + 1

        increment = 0
        last = data[length - 1]

        for index in range(0, length):
            rindex = length - index - 1
            nxt = data.get(rindex, last)
            if nxt >= last:
                if (max_increment > 0) and ((nxt - last) > max_increment):
                    # Smooth out big spikes
                    pass
                else:
                    increment += nxt - last
            last = nxt
            new_data[rindex] = increment

        return new_data

    def minutes_since_yesterday(self, now):
        """
        Calculate the number of minutes since 23:59 yesterday
        """
        yesterday = now - timedelta(days=1)
        yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
        difference = now - yesterday_at_2359
        difference_minutes = int((difference.seconds + 59) / 60)
        return difference_minutes

    def dp2(self, value):
        """
        Round to 2 decimal places
        """
        return round(value*100)/100

    def dp3(self, value):
        """
        Round to 3 decimal places
        """
        return round(value*1000)/1000

    def minute_data_entity(self, now_utc, key, incrementing=False):
        """
        Download one or more entities of data
        """
        entity_ids = self.get_arg(key, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        data_points = {}
        total_count = len(entity_ids)
        for entity_id in entity_ids:
            try:
                history = self.get_history(entity_id = entity_id, days = self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                data_points = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, scale=1.0 / total_count, clean_increment=incrementing, accumulate=data_points)
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))
                self.record_status("Warn - Unable to fetch history from {}".format(entity_id), had_errors=True)

        return data_points

    def update_pred(self, scheduled):
        self.had_errors = False

        local_tz = pytz.timezone(self.get_arg('timezone', "Europe/London"))
        now_utc = datetime.now(local_tz)
        now = datetime.now()

        self.log("--------------- PredHeat - update at {}".format(now_utc))
        self.max_days_previous = self.get_arg('max_days_previous', 7)

        self.external_temperature = self.minute_data_entity(now_utc, 'external_temperature')
        self.internal_temperature = self.minute_data_entity(now_utc, 'internal_temperature')
        self.target_temperature   = self.minute_data_entity(now_utc, 'target_temperature')
        self.heating_energy       = self.minute_data_entity(now_utc, 'heating_energy', incrementing=True)


    def select_event(self, event, data, kwargs):
        """
        Catch HA Input select updates
        """
        service_data = data.get('service_data', {})
        value = service_data.get('option', None)
        entities = service_data.get('entity_id', [])

        # Can be a string or an array        
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ('entity' in item) and (item['entity'] in entities):
                entity = item['entity']
                self.log("select_event: {} = {}".format(entity, value))
                self.expose_config(item['name'], value)
                self.update_pending = True
                return

    def number_event(self, event, data, kwargs):
        """
        Catch HA Input number updates
        """
        service_data = data.get('service_data', {})
        value = service_data.get('value', None)
        entities = service_data.get('entity_id', [])

        # Can be a string or an array        
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ('entity' in item) and (item['entity'] in entities):
                entity = item['entity']
                self.log("number_event: {} = {}".format(entity, value))
                self.expose_config(item['name'], value)
                self.update_pending = True
                return

    def switch_event(self, event, data, kwargs):
        """
        Catch HA Switch toggle
        """
        service = data.get('service', None)
        service_data = data.get('service_data', {})
        entities = service_data.get('entity_id', [])

        # Can be a string or an array        
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ('entity' in item) and (item['entity'] in entities):
                value = item['value']
                entity = item['entity']

                if service == 'turn_on':
                    value = True
                elif service == 'turn_off':
                    value = False
                elif service == 'toggle' and isinstance(value, bool):
                    value = not value
                
                self.log("switch_event: {} = {}".format(entity, value))
                self.expose_config(item['name'], value)
                self.update_pending = True
                return

    def get_ha_config(self, name):
        """
        Get Home assistant config
        """
        for item in CONFIG_ITEMS:
            if item['name'] == name:
                value = item.get('value')
                return value
        return None

    def expose_config(self, name, value):
        """
        Share the config with HA
        """
        for item in CONFIG_ITEMS:
            if item['name'] == name:
                entity = item.get('entity')
                if entity and ((item.get('value') is None) or (value != item['value'])):
                    item['value'] = value
                    self.log("Updating HA config {} to {}".format(name, value))
                    if item['type'] == 'input_number':
                        icon = item.get('icon', 'mdi:numeric')
                        self.set_state(entity_id = entity, state = value, attributes={'friendly_name' : item['friendly_name'], 'min' : item['min'], 'max' : item['max'], 'step' : item['step'], 'icon' : icon})
                    elif item['type'] == 'switch':
                        icon = item.get('icon', 'mdi:light-switch')
                        self.set_state(entity_id = entity, state = ('on' if value else 'off'), attributes = {'friendly_name' : item['friendly_name'], 'icon' : icon})
                    elif item['type'] == 'select':
                        icon = item.get('icon', 'mdi:format-list-bulleted')
                        self.set_state(entity_id = entity, state = value, attributes = {'friendly_name' : item['friendly_name'], 'options' : item['options'], 'icon' : icon})
                    elif item['type'] == 'update':
                        summary = self.releases.get('this_body', '')
                        latest = self.releases.get('latest', 'check HACS')
                        self.set_state(entity_id = entity, state = 'off', attributes = {'friendly_name' : item['friendly_name'], 'title' : item['title'], 'in_progress' : False, 'auto_update' : False, 
                                                                                        'installed_version' : item['installed_version'], 'latest_version' : latest, 'entity_picture' : item['entity_picture'], 
                                                                                        'release_url' : item['release_url'], 'skipped_version' : False, 'release_summary' : summary})

    def load_user_config(self):
        """
        Load config from HA
        """

        # Find values and monitor config
        for item in CONFIG_ITEMS:
            name = item['name']
            type = item['type']
            entity = type + "." + self.prefix + "_" + name
            item['entity'] = entity
            ha_value = None

            # Get from current state?
            if not self.args.get('user_config_reset', False):
                ha_value = self.get_state(entity)

                # Get from history?
                if ha_value is None:
                    history = self.get_history(entity_id = entity)
                    if history:
                        history = history[0]
                        ha_value = history[-1]['state']

            # Switch convert to text
            if type == 'switch' and isinstance(ha_value, str):
                if ha_value.lower() in ['on', 'true', 'enable']:
                    ha_value = True
                else:
                    ha_value = False

            if type == 'input_number' and ha_value is not None:
                try:
                    ha_value = float(ha_value)
                except (ValueError, TypeError):
                    ha_value = None

            if type == 'update':
                ha_value = None

            # Push back into current state
            if ha_value is not None:
                self.expose_config(item['name'], ha_value)
                
        # Register HA services
        self.fire_event('service_registered', domain="input_number", service="set_value")
        self.fire_event('service_registered', domain="input_number", service="increment")
        self.fire_event('service_registered', domain="input_number", service="decrement")
        self.fire_event('service_registered', domain="switch", service="turn_on")
        self.fire_event('service_registered', domain="switch", service="turn_off")
        self.fire_event('service_registered', domain="switch", service="toggle")        
        self.fire_event('service_registered', domain="select", service="select_option")
        self.fire_event('service_registered', domain="select", service="select_first")
        self.fire_event('service_registered', domain="select", service="select_last")
        self.fire_event('service_registered', domain="select", service="select_next")
        self.fire_event('service_registered', domain="select", service="select_previous")
        self.listen_select_handle = self.listen_event(self.switch_event, event='call_service', domain="switch", service='turn_on')
        self.listen_select_handle = self.listen_event(self.switch_event, event='call_service', domain="switch", service='turn_off')
        self.listen_select_handle = self.listen_event(self.switch_event, event='call_service', domain="switch", service='toggle')
        self.listen_select_handle = self.listen_event(self.number_event, event='call_service', domain="input_number", service='set_value')
        self.listen_select_handle = self.listen_event(self.number_event, event='call_service', domain="input_number", service='increment')
        self.listen_select_handle = self.listen_event(self.number_event, event='call_service', domain="input_number", service='decrement')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_option')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_first')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_last')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_next')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_previous')

    def resolve_arg_re(self, arg, arg_value, state_keys):
        """
        Resolve argument regular expression on list or string
        """
        matched = True

        if isinstance(arg_value, list):
            new_list = []
            for item_value in arg_value:
                item_matched, item_value = self.resolve_arg_re(arg, item_value, state_keys)
                if not item_matched:
                    self.log('WARN: Regular argument {} expression {} failed to match - disabling this item'.format(arg, item_value))
                    new_list.append(None)
                else:
                    new_list.append(item_value)
            arg_value = new_list
        elif isinstance(arg_value, str) and arg_value.startswith('re:'):
            matched = False
            my_re = '^' + arg_value[3:] + '$'
            for key in state_keys:
                res = re.search(my_re, key)
                if res:
                    if len(res.groups()) > 0:
                        self.log('Regular expression argument {} matched {} with {}'.format(arg, my_re, res.group(1)))
                        arg_value = res.group(1)
                        matched = True
                        break
                    else:
                        self.log('Regular expression argument {} Matched {} with {}'.format(arg, my_re, res.group(0)))
                        arg_value = res.group(0)
                        matched = True
                        break
        return matched, arg_value

    def auto_config(self):
        """
        Auto configure
        match arguments with sensors
        """

        states = self.get_state()
        state_keys = states.keys()
        disabled = []

        if 0:
            predheat_keys = []
            for key in state_keys:
                if 'predheat' in str(key):
                    predheat_keys.append(key)
            predheat_keys.sort()
            self.log("Keys:\n  - entity: {}".format('\n  - entity: '.join(predheat_keys)))

        # Find each arg re to match
        for arg in self.args:
            arg_value = self.args[arg]
            matched, arg_value = self.resolve_arg_re(arg, arg_value, state_keys)
            if not matched:
                self.log("WARN: Regular expression argument: {} unable to match {}, now will disable".format(arg, arg_value))
                disabled.append(arg)
            else:
                self.args[arg] = arg_value

        # Remove unmatched keys
        for key in disabled:
            del self.args[key]

    def state_change(self, entity, attribute, old, new, kwargs):
        """
        State change monitor
        """
        self.log("State change: {} to {}".format(entity, new))

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        self.log("Predheat: Startup")
        try:
            self.reset()
        except Exception as e:
            self.log("ERROR: Exception raised {}".format(e))
            self.record_status('ERROR: Exception raised {}'.format(e))
            raise e
            
        run_every = self.get_arg('run_every', 5) * 60
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_now = (now - midnight).seconds

        # Calculate next run time to exactly align with the run_every time
        seconds_offset = seconds_now % run_every
        seconds_next = seconds_now + (run_every - seconds_offset)
        next_time = midnight + timedelta(seconds=seconds_next)
        self.log("Predheat: Next run time will be {} and then every {} seconds".format(next_time, run_every))

        self.update_pending = True

        # And then every N minutes
        self.run_every(self.run_time_loop, next_time, run_every, random_start=0, random_end=0)
        self.run_every(self.update_time_loop, datetime.now(), 15, random_start=0, random_end=0)

    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        if self.update_pending and not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=False)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status('ERROR: Exception raised {}'.format(e))
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=True)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status('ERROR: Exception raised {}'.format(e))
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False


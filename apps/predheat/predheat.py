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

THIS_VERSION = 'v0.3'
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
MAX_INCREMENT = 100
PREDICT_STEP = 5

CONFIG_ITEMS = [
    {'name' : 'version',           'friendly_name' : 'Predheat Core Update',  'type' : 'update', 'title' : 'Predheat', 'installed_version' : THIS_VERSION, 'release_url' : 'https://github.com/springfall2008/predheat/releases/tag/' + THIS_VERSION, 'entity_picture' : 'https://user-images.githubusercontent.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png'},
    {'name' : 'test',              'friendly_name' : 'test', 'type' : 'switch'},
    {'name' : 'next_volume_temp',  'friendly_name' : 'Volume Temperature Next', 'type' : 'input_number', 'min' : -20,   'max' : 40,     'step' : 0.1,  'unit' : 'c'},
]

"""
Notes:

A BTU – or British Thermal Unit – is an approximation of the amount of energy required to heat 1lb (one pound) of by 1 degree Farenheit, and is roughly equal to 1.055 KJoules

Watts are defined as 1 Watt = 1 Joule per second (1W = 1 J/s) which means that 1 kW = 1000 J/s.

Calculate the kilowatt-hours (kWh) required to heat the water using the following formula: Pt = (4.2 × L × T ) ÷ 3600. 
Pt is the power used to heat the water, in kWh. L is the number of liters of water that is being heated and T is the difference in temperature from what you started with, listed in degrees Celsius

So, if the average water temperature across the radiator is 70 degrees C, the Delta T is 50 degrees C. A radiator’s power output is most often expressed in watts.
You can easily convert between Delta 50, 60 and 70, for example: If a radiator has a heat output of 5000 BTU at ΔT=60, to find the heat output at ΔT=50, you simply multiply the BTU by 0.789. 
If you have a radiator with a heat output of 5000 BTU at ΔT=60, to find the heat output at ΔT=70, you multiply the BTU by 1.223. 
If you have a radiator with a heat output of 5000 BTU at ΔT=50, to find the heat output at ΔT=60, you need to multiply the BTU by 1.264.


FLOMASTA TYPE 22 DOUBLE-PANEL DOUBLE CONVECTOR RADIATOR 600MM X 1200MM WHITE 6998BTU 2051W Delta T50°C - 7.16L
600 x 1000mm DOUBLE-PANEL - 5832BTU 1709W
500 x 900mm DOUBLE-PANEL  - 4519BTU 1324W

https://www.tlc-direct.co.uk/Technical/DataSheets/Quinn_Barlo/BarloPanelOutputsSpecs-Feb-2102.pdf

Delta correction factors (see below)

Steel panel radiators = 11 litres / kW

Key functions:

1. Table of raditors with BTUs (or maybe have size/type and calculate this) and capacity. 
2. Work out water content of heating system
3. Ability to set flow temperature either fixed or to sensor
4. User to enter house heat loss figure
   - Need a way to scan past data and calibrate this
   - Look at energy output of heating vs degrees C inside to compute heat loss
5. Take weather data from following days to predict forward outside temperature
6. Predict forward target heating temperature
7. Compute when heating will run, work out water temperature and heat output (and thus energy use)
   - Account for heating efficency COP
   - Maybe use table of COP vs outside temperature to help predict this for heat pumps?
8. Predict room temperatures
9. Sensor for heating predicted energy to link to Predbat if electric

"""

DELTA_CORRECTION = {
    75 : 1.69,
    70 : 1.55,
    65 : 1.41,
    60 : 1.27,
    55 : 1.13,
    50 : 1,
    45 : 0.87,
    40 : 0.75,
    35 : 0.63,
    30 : 0.51,
    25 : 0.41,
    20 : 0.3,
    15 : 0.21,
    10 : 0.12,
    5  : 0.05,
    0  : 0.00
}

GAS_EFFICIENCY = {
    0  : 0.995,
    10 : 0.995,
    20 : 0.99,
    30 : 0.98,
    40 : 0.95,
    50 : 0.90,
    60 : 0.88,
    70 : 0.87,
    80 : 0.86,
    90 : 0.85,
    100 : 0.84    
}

HEAT_PUMP_EFFICIENCY = {
    -20 : 2.10,
    -18 : 2.15,
    -16	: 2.2,
    -14	: 2.25,
    -12	: 2.3,
    -10	: 2.4,
    -8	: 2.5,
    -6	: 2.6,
    -4	: 2.7,
    -2	: 2.8,
    0	: 2.9,
    2	: 3.1,
    4	: 3.3,
    6	: 3.6,
    8	: 3.8,
    10	: 3.9,
    12	: 4.1,
    14	: 4.3,
    16  : 4.3,
    18  : 4.3,
    20  : 4.3
}
HEAT_PUMP_EFFICIENCY_MAX = 4.3

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
        self.days_previous = [7]
        self.days_previous_weight = [1]
        self.octopus_url_cache = {}

    def record_status(self, message, debug="", had_errors = False):
        """
        Records status to HA sensor
        """
        self.set_state(self.prefix + ".status", state=message, attributes = {'friendly_name' : 'Status', 'icon' : 'mdi:information', 'last_updated' : datetime.now(), 'debug' : debug})
        if had_errors:
            self.had_errors = True

    def minute_data(self, history, days, now, state_key, last_updated_key,
                    backwards=False, to_key=None, smoothing=False, clean_increment=False, divide_by=0, scale=1.0, accumulate=[], adjust_key=None, prev_last_updated_time=None, last_state=0):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        adata = {}
        newest_state = 0
        newest_age = 999999
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
                    if smoothing:
                        to_time = last_updated_time
                        last_updated_time = prev_last_updated_time
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
                        if clean_increment and state < last_state and (state <= (last_state / 10.0)):
                            while minute < minutes_to:
                                mdata[minute] = state
                                minute += 1
                        else:
                            # Can't really go backwards as incrementing data
                            if clean_increment and state < last_state:
                                state = last_state

                            # Create linear function
                            diff = (state - last_state) / (minutes_to - minute)

                            # If the spike is too big don't smooth it, it will removed in the clean function later
                            if clean_increment and max_increment > 0 and diff > max_increment:
                                diff = 0

                            index = 0
                            while minute < minutes_to:
                                if backwards:
                                    mdata[minute] = state - diff*index
                                else:
                                    mdata[minute] = last_state + diff*index
                                minute += 1
                                index += 1
                    else:
                        while minute < minutes_to:
                            if backwards:
                                mdata[minute] = last_state
                            else:
                                mdata[minute] = state
                            if adjusted:
                                adata[minute] = True
                            minute += 1
            else:
                mdata[minutes] = state

            # Store previous time & state
            if to_time and not backwards:
                prev_last_updated_time = to_time
            else:
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

    def get_weather_data(self, now_utc):

        entity_id = self.get_arg('weather', indirect=False)
        data = self.get_arg('weather', attribute='forecast')
        self.temperatures = {}

        if data:
            self.temperatures = self.minute_data(data, self.forecast_days, now_utc, 'temperature', 'datetime', backwards=False, smoothing=True, prev_last_updated_time=now_utc, last_state=self.external_temperature[0])
        else:
            self.log("WARN: Unable to fetch data for {}".format(entity_id))
            self.record_status("Warn - Unable to fetch data from {}".format(entity_id), had_errors=True)

    def minute_data_entity(self, now_utc, key, incrementing=False, smoothing=False, scaling=1.0):
        """
        Download one or more entities of data
        """
        entity_ids = self.get_arg(key, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        data_points = {}
        age_days = None
        total_count = len(entity_ids)
        for entity_id in entity_ids:
            try:
                history = self.get_history(entity_id = entity_id, days = self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                item = history[0][0]
                try:
                    last_updated_time = self.str2time(item['last_updated'])
                except (ValueError, TypeError):
                    last_updated_time = now_utc

                age = now_utc - last_updated_time
                if age_days is None:
                    age_days = age.days
                else:
                    age_days = min(age_days, age.days)

            if history:
                data_points = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=smoothing, scale=scaling / total_count, clean_increment=incrementing, accumulate=data_points)
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))
                self.record_status("Warn - Unable to fetch history from {}".format(entity_id), had_errors=True)

        if age_days is None:
            age_days = 0
        return data_points, age_days

    def get_from_incrementing(self, data, index):
        """
        Get a single value from an incrementing series e.g. kwh today -> kwh this minute
        """
        while index < 0:
            index += 24*60
        return data.get(index, 0) - data.get(index + 1, 0)

    def get_from_history(self, data, index):
        """
        Get a single value from a series e.g. temperature now
        """
        while index < 0:
            index += 24*60
        return data.get(index, 0)

    def get_historical(self, data, minute):
        """
        Get historical data across N previous days in days_previous array based on current minute 
        """
        total = 0
        total_weight = 0
        this_point = 0

        # No data?
        if not data:
            return 0

        for days in self.days_previous:
            use_days = min(days, self.minute_data_age)
            weight = self.days_previous_weight[this_point]                
            if use_days > 0:
                full_days = 24*60*(use_days - 1)
                minute_previous = 24 * 60 - minute + full_days
                value = self.get_from_history(data, minute_previous)
                total += value * weight
                total_weight += weight
            this_point += 1
    
        # Zero data?
        if total_weight == 0:
            return 0
        else:
            return total / total_weight

    def run_simulation(self, volume_temp, heating_active, save='best', last_predict_minute=None):

        internal_temp = self.internal_temperature[0]
        external_temp = self.external_temperature[0]
        internal_temp_predict_stamp = {}
        external_temp_predict_stamp = {}
        internal_temp_predict_minute = {}
        target_temp_predict_stamp = {}
        target_temp_predict_minute = {}
        heat_energy = self.heat_energy_today
        heat_energy_predict_minute = {}
        heat_energy_predict_stamp = {}
        heat_to_predict_stamp = {}
        heat_to_predict_temperature = {}
        heating_on = False
        next_volume_temp = volume_temp
        volume_temp_stamp = {}
        WATTS_TO_DEGREES = 1.16
        cost = self.import_today_cost
        cost_stamp = {}
        cost_minute = {}
        energy_today_external = []
        adjustment_points = []

        self.log("External temp now {}".format(self.temperatures.get(0, external_temp)))

        # Find temperature adjustment points (thermostat turned up)
        adjust_ptr = -1
        if last_predict_minute:
            last_target_temp = self.get_historical(self.target_temperature, 0)
            for minute in range(0, self.forecast_days*24*60, PREDICT_STEP):
                target_temp = self.get_historical(self.target_temperature, minute)
                if target_temp > last_target_temp:
                    adjust = {}
                    adjust['from'] = last_target_temp
                    adjust['to'] = target_temp
                    adjust['end'] = minute
                    adjust_ptr = 0

                    reached_minute = minute
                    for next_minute in range(minute, self.forecast_days*24*60, PREDICT_STEP):
                        if last_predict_minute[next_minute] >= target_temp:
                            reached_minute = next_minute
                            break
                    adjust['reached'] = reached_minute
                    timeframe = reached_minute - minute
                    adjust['timeframe'] = timeframe
                    adjust['start'] = max(minute - timeframe, 0)
                    adjustment_points.append(adjust)
                last_target_temp = target_temp
            self.log("Thermostat adjusts {}".format(adjustment_points))

        for minute in range(0, self.forecast_days*24*60, PREDICT_STEP):
            minute_absolute = minute + self.minutes_now
            external_temp = self.temperatures.get(minute, external_temp)

            target_temp = self.get_historical(self.target_temperature, minute)

            # Find the next temperature adjustment
            next_adjust = None
            if adjust_ptr >= 0:
                next_adjust = adjustment_points[adjust_ptr]
            if next_adjust and minute > adjust['end']:
                adjust_ptr += 1
                if adjust_ptr >= len(adjustment_points):
                    adjust_ptr = -1
                    next_adjust = None
                else:
                    next_adjust = adjustment_points[adjust_ptr]

            if self.smart_thermostat:
                if next_adjust and minute >= adjust['start'] and minute < adjust['end']:
                    target_temp = adjust['to']
                    self.log("Adjusted target temperature for smart heating to {} at minute {}".format(target_temp, minute))

            temp_diff_outside = internal_temp - external_temp
            temp_diff_inside = target_temp - internal_temp


            # Thermostat model, override with current state also
            if minute == 0:
                heating_on = heating_active
            elif temp_diff_inside >= 0.1:
                heating_on = True
            elif temp_diff_inside <= 0:
                heating_on = False

            heat_loss_current = self.heat_loss_watts * temp_diff_outside * PREDICT_STEP / 60.0
            heat_loss_current -= self.heat_gain_static * PREDICT_STEP / 60.0

            flow_temp = 0
            heat_to = 0
            heat_power_in = 0
            heat_power_out = 0
            if heating_on:
                heat_to = target_temp
                flow_temp = self.flow_temp
                if volume_temp < flow_temp:
                    flow_temp_diff = min(flow_temp - volume_temp, self.flow_difference_target)
                    power_percent = flow_temp_diff / self.flow_difference_target
                    heat_power_in = self.heat_max_power * power_percent
                    heat_power_in = max(self.heat_min_power, heat_power_in)
                    heat_power_in = min(self.heat_max_power, heat_power_in)

                    # self.log("Minute {} flow {} volume {} diff {} power {} kw".format(minute, flow_temp, volume_temp, flow_temp_diff, heat_power_in / 1000.0))

                energy_now = heat_power_in * PREDICT_STEP / 60.0 / 1000.0
                cost += energy_now + self.rate_import.get(minute_absolute, 0)

                heat_energy += energy_now
                heat_power_out = heat_power_in * self.heat_cop

                if self.mode == 'gas':
                    # Gas boiler flow temperature adjustment in efficiency based on flow temp
                    inlet_temp = int(volume_temp / 10 + 0.5) * 10
                    condensing = GAS_EFFICIENCY.get(inlet_temp, 0.80)
                    heat_power_out *= condensing
                else:
                    # Heat pump efficiency based on outdoor temp
                    out_temp = int(external_temp / 2 + 0.5) * 2
                    cop_adjust = HEAT_PUMP_EFFICIENCY.get(out_temp, 2.0) / HEAT_PUMP_EFFICIENCY_MAX
                    heat_power_out *= cop_adjust

                # 1.16 watts required to raise water by 1 degree in 1 hour
                volume_temp += (heat_power_out / WATTS_TO_DEGREES / self.heat_volume) * PREDICT_STEP / 60.0
            
            flow_delta = volume_temp - internal_temp
            flow_delta_rounded = int(flow_delta / 5 + 0.5) * 5
            flow_delta_rounded = max(flow_delta_rounded, 0)
            flow_delta_rounded = min(flow_delta_rounded, 75)
            correction = DELTA_CORRECTION.get(flow_delta_rounded, 0)
            heat_output = self.heat_output * correction

            # Cooling of the radiators
            volume_temp -= (heat_output / WATTS_TO_DEGREES / self.heat_volume) * PREDICT_STEP / 60.0

            heat_loss_current -= heat_output * PREDICT_STEP / 60.0

            internal_temp = internal_temp - heat_loss_current / self.watt_per_degree             

            # Store for charts
            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                internal_temp_predict_stamp[stamp] = self.dp2(internal_temp)
                external_temp_predict_stamp[stamp] = self.dp2(external_temp)
                target_temp_predict_stamp[stamp] = self.dp2(target_temp)
                heat_to_predict_stamp[stamp] = self.dp2(heat_to)
                heat_energy_predict_stamp[stamp] = self.dp2(heat_energy)
                volume_temp_stamp[stamp] = self.dp2(volume_temp)
                cost_stamp[stamp] = self.dp2(cost)

                entry = {}
                entry['last_updated'] = stamp
                entry['energy'] = self.dp2(heat_energy)
                energy_today_external.append(entry)
            
            # Store raw data
            target_temp_predict_minute[minute] = self.dp2(target_temp)
            internal_temp_predict_minute[minute] = self.dp2(internal_temp)
            heat_to_predict_temperature[minute] = self.dp2(heat_to)
            heat_energy_predict_minute[minute] = self.dp2(heat_energy)
            cost_minute[minute] = self.dp2(cost)
            
            if minute == 0:
                next_volume_temp = volume_temp            

        if save == 'best':
            self.set_state(self.prefix + ".internal_temp", state=self.dp2(self.internal_temperature[0]), attributes = {'results' : internal_temp_predict_stamp, 'friendly_name' : 'Internal Temperature Predicted', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".external_temp", state=self.dp2(self.external_temperature[0]), attributes = {'results' : external_temp_predict_stamp, 'friendly_name' : 'External Temperature Predicted', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".target_temp", state=self.dp2(target_temp_predict_minute[0]), attributes = {'results' : target_temp_predict_stamp, 'friendly_name' : 'Target Temperature Predicted', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".heat_to_temp", state=self.dp2(heat_to_predict_temperature[0]), attributes = {'results' : heat_to_predict_stamp, 'friendly_name' : 'Predict heating to target', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".internal_temp_h1", state=self.dp2(internal_temp_predict_minute[60]), attributes = {'friendly_name' : 'Internal Temperature Predicted +1hr', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".internal_temp_h2", state=self.dp2(internal_temp_predict_minute[60 * 2]), attributes = {'friendly_name' : 'Internal Temperature Predicted +2hr', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".internal_temp_h8", state=self.dp2(internal_temp_predict_minute[60 * 8]), attributes = {'friendly_name' : 'Internal Temperature Predicted +8hrs', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".heat_energy", state=self.heat_energy_today, attributes = {'external' : energy_today_external, 'results' : heat_energy_predict_stamp, 'friendly_name' : 'Predict heating energy', 'state_class': 'measurement', 'unit_of_measurement': 'kWh'})
            self.set_state(self.prefix + ".heat_energy_h1", state=heat_energy_predict_minute[60], attributes = {'friendly_name' : 'Predict heating energy +1hr', 'state_class': 'measurement', 'unit_of_measurement': 'kWh'})
            self.set_state(self.prefix + ".heat_energy_h2", state=heat_energy_predict_minute[60 * 2], attributes = {'friendly_name' : 'Predict heating energy +2hrs', 'state_class': 'measurement', 'unit_of_measurement': 'kWh'})
            self.set_state(self.prefix + ".heat_energy_h8", state=heat_energy_predict_minute[60 * 8], attributes = {'friendly_name' : 'Predict heating energy +8hrs', 'state_class': 'measurement', 'unit_of_measurement': 'kWh'})
            self.set_state(self.prefix + ".volume_temp", state=self.dp2(next_volume_temp), attributes = {'results' : volume_temp_stamp, 'friendly_name' : 'Volume temperature', 'state_class': 'measurement', 'unit_of_measurement': 'c'})
            self.set_state(self.prefix + ".cost", state=self.dp2(cost), attributes = {'results' : cost_stamp, 'friendly_name' : 'Predicted cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
            self.set_state(self.prefix + ".cost_h1", state=self.dp2(cost_minute[60]), attributes = {'friendly_name' : 'Predicted cost +1hr', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
            self.set_state(self.prefix + ".cost_h2", state=self.dp2(cost_minute[60 * 2]), attributes = {'friendly_name' : 'Predicted cost +2hrs', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
            self.set_state(self.prefix + ".cost_h8", state=self.dp2(cost_minute[60 * 8]), attributes = {'friendly_name' : 'Predicted cost +8hrs', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
        return next_volume_temp, internal_temp_predict_minute

    def rate_replicate(self, rates, rate_io={}, is_import=True):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = 0
        rate_last = 0
        adjusted_rates = {}

        # Add 48 extra hours to make sure the whole cycle repeats another day
        while minute < (self.forecast_minutes + 48*60):
            if minute not in rates:
                # Take 24-hours previous if missing rate
                if (minute >= 24*60) and ((minute - 24*60) in rates):
                    minute_mod = minute - 24*60
                else:
                    minute_mod = minute % (24 * 60)

                if (minute_mod in rate_io) and rate_io[minute_mod]:
                    # Dont replicate Intelligent rates into the next day as it will be different
                    rate_offset = self.rate_max
                elif minute_mod in rates:
                    rate_offset = rates[minute_mod]
                else:
                    # Missing rate within 24 hours - fill with dummy last rate
                    rate_offset = rate_last

                # Only offset once not every day
                if minute_mod not in adjusted_rates:
                    if is_import:
                        rate_offset = rate_offset + self.metric_future_rate_offset_import
                    else:
                        rate_offset = max(rate_offset + self.metric_future_rate_offset_export, 0)
                    adjusted_rates[minute] = True

                rates[minute] = rate_offset
            else:
                rate_last = rates[minute]
            minute += 1
        return rates

    def basic_rates(self, info, rtype, prev=None):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        if prev:
            rates = prev.copy()
            self.log("Override {} rate info {}".format(rtype, info))
        else:
            # Set to zero
            self.log("Adding {} rate info {}".format(rtype, info))
            for minute in range(0, 24*60):
                rates[minute] = 0

        max_minute = max(rates) + 1
        midnight = datetime.strptime('00:00:00', "%H:%M:%S")
        for this_rate in info:
            start = datetime.strptime(this_rate.get('start', "00:00:00"), "%H:%M:%S")
            end = datetime.strptime(this_rate.get('end', "00:00:00"), "%H:%M:%S")
            date = None
            if 'date' in this_rate:
                date = datetime.strptime(this_rate['date'], "%Y-%m-%d")
            rate = this_rate.get('rate', 0)

            # Time in minutes
            start_minutes = max(self.minutes_to_time(start, midnight), 0)
            end_minutes   = min(self.minutes_to_time(end, midnight), 24*60-1)

            # Make end > start
            if end_minutes <= start_minutes:
                end_minutes += 24*60

            # Adjust for date if specified
            if date:
                delta_minutes = self.minutes_to_time(date, self.midnight)
                start_minutes += delta_minutes
                end_minutes += delta_minutes

            # Store rates against range
            if end_minutes >= 0 and start_minutes < max_minute:
                for minute in range(start_minutes, end_minutes):
                    if (not date) or (minute >= 0 and minute < max_minute):
                        rates[minute % max_minute] = rate

        return rates

    def today_cost(self, import_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_cost_import = 0
        day_energy = 0
        day_cost_time = {}

        for minute in range(0, self.minutes_now):
            # Add in standing charge
            if (minute % (24*60)) == 0:
                day_cost += self.metric_standing_charge

            minute_back = self.minutes_now - minute - 1
            energy = 0
            energy = self.get_from_incrementing(import_today, minute_back)
            day_energy += energy
            
            if self.rate_import:
                day_cost += self.rate_import[minute] * energy

            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = self.dp2(day_cost)

        self.set_state(self.prefix + ".cost_today", state=self.dp2(day_cost), attributes = {'results' : day_cost_time, 'friendly_name' : 'Cost so far today', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        self.log("Todays energy import {} kwh cost {} p".format(self.dp2(day_energy), self.dp2(day_cost), self.dp2(day_cost_import)))
        return day_cost

    def fetch_octopus_rates(self, entity_id, adjust_key=None):
        data_all = []
        rate_data = {}

        if entity_id:                
            # From 9.0.0 of the Octopus plugin the data is split between previous rate, current rate and next rate
            # and the sensor is replaced with an event - try to support the old settings and find the new events

            # Previous rates
            if '_current_rate' in entity_id:
                # Try as event
                prev_rate_id = entity_id.replace('_current_rate', '_previous_day_rates').replace('sensor.', 'event.')
                data_import = self.get_state(entity_id=prev_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    prev_rate_id = entity_id.replace('_current_rate', '_previous_rate')
                    data_import = self.get_state(entity_id=prev_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import

            # Current rates
            current_rate_id = entity_id.replace('_current_rate', '_current_day_rates').replace('sensor.', 'event.')
            data_import = self.get_state(entity_id=current_rate_id, attribute="rates")
            if data_import:
                data_all += data_import
            else:
                data_import = self.get_state(entity_id=entity_id, attribute="all_rates")
                if data_import:
                    data_all += data_import

            # Next rates
            if '_current_rate' in entity_id:
                next_rate_id = entity_id.replace('_current_rate', '_next_day_rates').replace('sensor.', 'event.')
                data_import = self.get_state(entity_id=next_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    next_rate_id = entity_id.replace('_current_rate', '_next_rate')
                    data_import = self.get_state(entity_id=next_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import

        if data_all:
            rate_key = "rate"
            from_key = "from"
            to_key = "to"
            scale = 1.0
            if rate_key not in data_all[0]:
                rate_key = "value_inc_vat"
                from_key = "valid_from"
                to_key = "valid_to"
            if from_key not in data_all[0]:
                from_key = "start"
                to_key = "end"
                scale = 100.0
            rate_data = self.minute_data(
                data_all, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key, adjust_key=adjust_key, scale=scale
            )

        return rate_data

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, age.seconds / 60))
                return pdata

        # Retry up to 3 minutes
        for retry in range(0, 3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("WARN: Unable to download Octopus data from URL {}".format(url))
            self.record_status("Warn - Unable to download Octopus data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.octopus_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        # Cache New Octopus data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["data"] = pdata
        return pdata

    def download_octopus_rates_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0

        while url and pages < 3:
            if self.debug_enable:
                self.log("Download {}".format(url))
            r = requests.get(url)
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                self.record_status("Warn - Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if "results" in data:
                mdata += data["results"]
            else:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                self.record_status("Warn - Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            url = data.get("next", None)
            pages += 1
        pdata = self.minute_data(mdata, self.forecast_days + 1, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return pdata

    def update_pred(self, scheduled):
        """
        Update the heat prediction 
        """
        self.had_errors = False

        local_tz = pytz.timezone(self.get_arg('timezone', "Europe/London"))
        now_utc = datetime.now(local_tz)
        now = datetime.now()
        self.forecast_days = self.get_arg('forecast_days', 2)
        self.forecast_minutes = self.forecast_days*60*24
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = int((now - self.midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        self.metric_future_rate_offset_import = 0

        self.log("--------------- PredHeat - update at {}".format(now_utc))
        self.days_previous = self.get_arg('days_previous', [7])
        self.days_previous_weight = self.get_arg('days_previous_weight', [1 for i in range(0, len(self.days_previous))])
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(0, len(self.days_previous) - len(self.days_previous_weight))]
        self.max_days_previous = max(self.days_previous) + 1
        self.heating_energy_scaling = self.get_arg('heating_energy_scaling', 1.0)

        self.external_temperature, age_external = self.minute_data_entity(now_utc, 'external_temperature', smoothing=True)
        self.internal_temperature, age_internal = self.minute_data_entity(now_utc, 'internal_temperature', smoothing=True)
        self.target_temperature,   age_target   = self.minute_data_entity(now_utc, 'target_temperature')
        self.heating_energy,       age_energy  = self.minute_data_entity(now_utc, 'heating_energy', incrementing=True, smoothing=True, scaling=self.heating_energy_scaling)
        self.minute_data_age      = min(age_external, age_internal, age_target, age_energy)

        self.heat_energy_today    = self.heating_energy.get(0, 0) - self.heating_energy.get(self.minutes_now, 0)

        self.mode                 = self.get_arg('mode', 'pump')
        self.flow_temp            = self.get_arg('flow_temp', 40.0)
        self.flow_difference_target = self.get_arg('float_difference_target', 20.0)
        self.log("We have {} days of historical data".format(self.minute_data_age))
        self.heat_loss_watts      = self.get_arg('heat_loss_watts', 100)
        self.heat_loss_degrees    = self.get_arg('heat_loss_degrees', 0.02)
        self.heat_gain_static     = self.get_arg('heat_gain_static', 0)
        self.watt_per_degree      = self.heat_loss_watts / self.heat_loss_degrees
        self.heat_output          = self.get_arg('heat_output', 7000)
        self.heat_volume          = self.get_arg('heat_volume', 200)
        self.heat_max_power       = self.get_arg('heat_max_power', 30000)
        self.heat_min_power       = self.get_arg('heat_min_power', 7000)
        self.heat_cop             = self.get_arg('heat_cop', 0.9)
        self.next_volume_temp     = self.get_arg('next_volume_temp', self.internal_temperature[0])
        self.smart_thermostat     = self.get_arg('smart_thermostat', False)

        self.heating_active       = self.get_arg('heating_active', False)

        self.log("Heating active {} Heat loss watts {} degrees {} watts per degree {} heating energy so far {}".format(self.heating_active, self.heat_loss_watts, self.heat_loss_degrees, self.watt_per_degree, self.heat_energy_today))
        self.get_weather_data(now_utc)
        status = 'idle'

        if "rates_import_octopus_url" in self.args:
            # Fixed URL for rate import
            self.log("Downloading import rates directly from url {}".format(self.get_arg("rates_import_octopus_url", indirect=False)))
            self.rate_import = self.download_octopus_rates(self.get_arg("rates_import_octopus_url", indirect=False))
        elif "metric_octopus_import" in self.args:
            # Octopus import rates
            entity_id = self.get_arg("metric_octopus_import", None, indirect=False)
            self.rate_import = self.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")             
            if not self.rate_import:
                self.log("Error: metric_octopus_import is not set correctly or no energy rates can be read")
                self.record_status(message="Error - metric_octopus_import not set correctly or no energy rates can be read", had_errors=True)
                raise ValueError
        else:
            # Basic rates defined by user over time
            self.rate_import = self.basic_rates(self.get_arg("rates_import", [], indirect=False), "import")

        # Standing charge
        self.metric_standing_charge = self.get_arg('metric_standing_charge', 0.0) * 100.0
        self.log("Standing charge is set to {} p".format(self.metric_standing_charge))

        # Replicate rates
        self.rate_import = self.rate_replicate(self.rate_import)

        # Cost so far today
        self.import_today_cost = self.today_cost(self.heating_energy)

        # Run sim
        next_volume_temp, predict_minute = self.run_simulation(self.next_volume_temp, self.heating_active)
        next_volume_temp, predict_minute = self.run_simulation(self.next_volume_temp, self.heating_active, last_predict_minute=predict_minute, save='best')
        if scheduled:
            # Update state
            self.next_volume_temp = next_volume_temp
            self.expose_config('next_volume_temp', self.next_volume_temp)
            self.log("Updated next_volume_temp to {}".format(self.next_volume_temp))

        if self.had_errors:
            self.log("Completed run status {} with Errors reported (check log)".format(status))
        else:
            self.log("Completed run status {}".format(status))
            self.record_status(status)


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
            self.auto_config()
            self.load_user_config()
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


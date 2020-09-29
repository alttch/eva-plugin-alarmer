# eva-plugin-alarmer

Alarms plugin for [EVA ICS](https://www.eva-ics.com/)

## Installation

This is a single-file plugin, put "alarmer.py" into EVA ICS plugins folder and
enjoy.

The plugin depends on
[eva-plugin-userinfo](https://github.com/alttch/eva-plugin-userinfo), which
should be also installed.

## Configuration

The plugin requires installation into both LM PLC (processes alarms) and SFA
(provides management API).

LM PLC:

```ini
[alarmer]
db = runtime/db/somedatabase.db ; SAME database as for userinfo plugin
keep_log = 86400 ; period to keep alarm log records (seconds)
userinfo_email_field = email ; userinfo plugin field containing user email
```

As the plugin sends email notifications, *[mailer]* section of *lm.ini* should
be also properly configured.

SFA:

```ini
[alarmer]
lm = mws1 ; ID of LM PLC connected to SFA
db = runtime/db/somedatabase.db ; SAME database as specified before
```

## Architecture and logic

Alarms can have 3 levels, which are represented into the corresponding lvar
value:

* 0 (or empty) - the alarm is unset / acknowledged
* 1 - the alarm is in the warning state
* 2 - the alarm is in the alarm state

Each alarm includes 3 elements: one logical variable (name is equal to alarm
ID, created in "alarmer" supergroup) and two decision matrix rules: one for the
warning state (ending with "\_w") and another one for the alarm state (ending
with "\_a"). The rules can monitor same EVA ICS item or different ones.

When alarm rule matches an event, the alarm is triggered and lvar value is set.
Also, if there are users subscribed, email notifications are sent to their
emails (the addresses should be set with "userinfo" plugin).

Alarm IDs are auto-generated.

When alarm is triggered, an exposed function "x_alarmer_notify" is called with
two params: alarm id and alarm level (1 or 2). The exposed function can be
called from custom LM PLC macros as well.

If the alarm lvar has "0" status, the alarm is considered as disabled.

The alarm log is stored in the specified database and contains all available
info about alarm actions. The field "action" code value "T" means alarm was
triggered, "A" is for acknowledged.

## Exposed API methods

### Management

* **x\_alarmer\_create**(d, g, w, a, save) - creates new alarm, requires the
  master key

    * w - warning rule props (same as for set_rule_prop LM PLC API call)
    * a - alarm rule props (same as for set_rule_prop LM PLC API call)
    * d - alarm description (optional)
    * g - alarm group (optional)
    * save - auto-save lvar/rules after creation (usually true)

* **x\_alarmer\_set\_description**(i, d, save) - change alarm description,
  requires the master key

    * i - alarm id
    * d - new description
    * save - auto-save lvar (usually true)

* **x\_alarmer\_set\_rule_props**(i, w, a, save) - change alarm rule
  properties, requires the master key

    * i - alarm id
    * w - warning rule props (optional)
    * a - alarm rule props (optional)
    * save - auto-save rules (usually true)

* **x\_alarmer\_list\_rule_props**(i) - list alarm rule
  properties, requires the master key

    * i - alarm id

* **x\_alarmer\_destroy**(i, w, a, save) - deletes alarm, requires the master
  key

    * i - alarm id

### User functions

* **x\_alarmer\_ack**(i) - acknowledges alarm, the user (or API key) must have
  an access to the alarm logical variable.

    * i - alarm id

* **x\_alarmer\_get\_log**(i, n) - get alarm log, the user (or API key) must
  have an access to the alarm logical variable.

    * i - alarm id (required for user, optional for master key)
    * n - max number of records to get (default: 100)

* **x\_alarmer\_subscribe**(i, l) - subscribe to the alarm, the user MUST be
  logged in and have an access to alarm lvar (at least read-only)

    * i - alarm id
    * l - alarm level (1 or 2, required)

* **x\_alarmer\_unsubscribe**(i) - unsubscribe from the alarm, the user MUST be
  logged in and have an access to alarm lvar (at least read-only)

    * i - alarm id

There's no function to list alarms. To get that info, just list lvars in
"alarmer" supergroup.


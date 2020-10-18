__author__ = 'Altertech, https://www.altertech.com/'
__copyright__ = 'Copyright (C) 2012-2020 Altertech'
__license__ = 'Apache License 2.0'
__version__ = '0.0.2'

import eva.pluginapi as pa
import sqlalchemy as sa
import threading
import time

from neotasker import g, background_worker

from eva.client import apiclient
from functools import partial

# undocummented internal function, don't use in own plugins
import eva.mailer
import eva.core

sql = sa.text

from types import SimpleNamespace
db_lock = threading.RLock()
flags = SimpleNamespace(ready=False, db=None)

logger = pa.get_logger()


# undocummented thread-local, don't use in own plugins
def get_db():
    with db_lock:
        if not g.has('x_alarmer_db'):
            g.x_alarmer_db = flags.db.connect()
        else:
            try:
                g.x_alarmer_db.execute('select 1')
            except:
                try:
                    g.userdb.close()
                except:
                    pass
                g.x_alarmer_db = flags.db.connect()
        return g.x_alarmer_db


def get_level_name(level):
    return 'WARNING' if level == 1 else 'ALARM'


def notify(alarm_id, level):
    level = int(level)
    lv = pa.api_call('state', i=f'lvar:alarmer/{alarm_id}', full=True)
    db = get_db()
    try:
        db.execute(sql(
            'insert into alarmer_log'
            '(u, utp, key_id, alarm_id, description, action, t, level)'
            'values (:u, :utp, :key_id, :alarm_id, :d, :action, :t, :level)'),
                   u='',
                   utp='',
                   key_id='',
                   alarm_id=alarm_id,
                   d=lv['description'],
                   action='T',
                   t=time.time(),
                   level=level)
    except:
        logger.error(f'Unable to insert log record for alarm: {alarm_id}')
        pa.log_traceback()
    try:
        if lv['status'] == 1:
            cur_value = lv['value']
            if cur_value:
                cur_value = int(cur_value)
            else:
                cur_value = 0
            if cur_value >= level:
                logger.info('Skipping alarm notifications, '
                            f'already triggered: {alarm_id}')
            else:
                pa.api_call('set', i=f'lvar:alarmer/{alarm_id}', v=level)
                logger.warning('Alarm triggered: '
                               f'{alarm_id}, level: {get_level_name(level)}')
                r = db.execute(sql('select u, utp from alarmer_sub '
                                   'where alarm_id=:i and level<=:level'),
                               i=alarm_id,
                               level=level)
                recip = []
                subject = f'{get_level_name(level)}: {lv["description"]}'
                text = (f'{get_level_name(level)}: {lv["description"]} '
                        f'({alarm_id})\n'
                        f'System: {eva.core.config.system_name}')
                sendmail = partial(eva.mailer.send, subject=subject, text=text)
                while True:
                    ui = r.fetchone()
                    if ui:
                        r2 = db.execute(sql('select value from userinfo where '
                                            'name=:name and u=:u and utp=:utp'),
                                        name=flags.userinfo_email_field,
                                        u=ui.u,
                                        utp=ui.utp)
                        while True:
                            d = r2.fetchone()
                            if d:
                                logger.debug(
                                    f'sending alarm email to {d.value}')
                                sendmail(rcp=recip)
                            else:
                                break
                    else:
                        break
        else:
            logger.debug(f'Inactive alarm triggered: {alarm_id}')
    except:
        logger.error(f'Unable to send notifications for alarm: {alarm_id}')
        pa.log_traceback()
        raise


def init(config, **kwargs):
    logger.debug('alarmer plugin loaded')
    pa.register_apix(APIFuncs(), sys_api=False)
    p = pa.get_product()
    # undocummented internal function, don't use in own plugins
    from eva.core import create_db_engine, format_db_uri
    db = format_db_uri(config['db'])
    flags.db = create_db_engine(db)
    logger.debug(f'alarmer.db = {db}')
    if p.code == 'lm':
        pa.register_lmacro_object('notify', notify)
        flags.keep_log = int(config.get('keep_log', 86400))
        logger.debug(f'alarmer.keep_log = {flags.keep_log}')
        flags.userinfo_email_field = config.get('userinfo_email_field', 'email')
        logger.debug(
            f'alarmer.userinfo_email_field = {flags.userinfo_email_field}')
    elif p.code == 'sfa':
        lm = config['lm']
        if not lm.startswith('lm/'):
            lm = 'lm/' + lm
        logger.debug(f'alarmer.lm = {lm}')
        flags.lm = lm
        pa.register_apix(APIFuncs(), sys_api=False)
    else:
        RuntimeError(f'product not supported: {p}')
    flags.ready = True


def before_start(**kwargs):
    dbconn = get_db()
    meta = sa.MetaData()
    t_alarmer_sub = sa.Table(
        'alarmer_sub', meta, sa.Column('u', sa.String(128), primary_key=True),
        sa.Column('utp', sa.String(32), primary_key=True),
        sa.Column('alarm_id', sa.String(256), primary_key=True),
        sa.Column('level', sa.Integer()))
    t_alarmer_log = sa.Table(
        'alarmer_log', meta, sa.Column('u', sa.String(128), primary_key=True),
        sa.Column('utp', sa.String(32), primary_key=True),
        sa.Column('key_id', sa.String(64), primary_key=True),
        sa.Column('alarm_id', sa.String(256), primary_key=True),
        sa.Column('description', sa.String(256), primary_key=True),
        sa.Column('action', sa.String(1), primary_key=True),
        sa.Column('t', sa.Float(), primary_key=True),
        sa.Column('level', sa.Integer(), primary_key=True))
    try:
        meta.create_all(dbconn)
    except:
        pa.log_traceback()
        logger.error('unable to create alarme tables in db')


def start(**kwargs):
    if pa.get_product().code == 'lm':
        log_cleaner.start()


def stop(**kwargs):
    if pa.get_product().code == 'lm':
        log_cleaner.stop()


class APIFuncs(pa.APIX):
    """
    ACL:
        - to receive alarm events in UI, user must have r/o access to alarm
          lvar
        - to disable/enable/acknowledge user must have rw access to alarm lvar
        - to create / edit / destroy alarms master key is required

    If alarm lvar has status = 0, the plugin considers the alarm as disabled
    """

    @pa.api_log_i
    def subscribe(self, **kwargs):
        k, i, l = pa.parse_function_params(kwargs, 'kil', 'SSI')
        lvar = pa.get_item(f'lvar:alarmer/{i}')
        if l < 1 or l > 2:
            raise pa.InvalidParameter('param "l" should be 1 or 2')
        if not lvar:
            raise pa.ResourceNotFound
        if not pa.key_check(k, lvar, ro_op=True):
            raise pa.AccessDenied
        db = get_db()
        u = pa.get_aci('u')
        if not u:
            raise pa.FunctionFailed('user is not logged in')
        utp = pa.get_aci('utp')
        if not utp:
            utp = ''
        kw = {'u': u, 'utp': utp, 'alarm_id': i, 'level': l}
        if db.execute(
                sql('select alarm_id from alarmer_sub where u=:u '
                    'and utp=:utp and alarm_id=:alarm_id'), **kw).fetchone():
            db.execute(
                sql('update alarmer_sub set level=:level '
                    'where u=:u and utp=:utp and alarm_id=:alarm_id'), **kw)
        else:
            db.execute(
                sql('insert into alarmer_sub(u, utp, alarm_id, level) '
                    'values (:u, :utp, :alarm_id, :level)'), **kw)
        return True

    @pa.api_log_i
    def unsubscribe(self, **kwargs):
        k, i = pa.parse_function_params(kwargs, 'ki', 'SS')
        lvar = pa.get_item(f'lvar:alarmer/{i}')
        if not lvar:
            raise pa.ResourceNotFound
        if not pa.key_check(k, lvar, ro_op=True):
            raise pa.AccessDenied
        db = get_db()
        u = pa.get_aci('u')
        if not u:
            raise pa.FunctionFailed('user is not logged in')
        utp = pa.get_aci('utp')
        if not utp:
            utp = ''
        kw = {'u': u, 'utp': utp, 'alarm_id': i}
        db.execute(
            sql('delete from alarmer_sub where u=:u '
                'and utp=:utp and alarm_id=:alarm_id'), **kw)
        return True

    @pa.api_log_i
    def list_subscriptions(self, **kwargs):
        u = pa.get_aci('u')
        if not u:
            raise pa.FunctionFailed('user is not logged in')
        utp = pa.get_aci('utp')
        if not utp:
            utp = ''
        kw = {'u': u, 'utp': utp}
        db = get_db()
        return [
            dict(x) for x in db.execute(
                sql('select alarm_id, level '
                    'from alarmer_sub where u=:u and utp=:utp'), **kw)
        ]

    @pa.api_log_i
    @pa.api_need_master
    def create(self, **kwargs):
        d, g, rw, ra, save = pa.parse_api_params(kwargs, 'dgwaS', 'ssRRb')
        import uuid
        alarm_id = str(uuid.uuid4())
        alarm_full_id = f'{g if g else ""}{"/" if g else ""}{alarm_id}'
        lvar_id = f'alarmer{"/" if g else ""}{g if g else ""}/{alarm_id}'
        try:
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='create_lvar',
                                 p={
                                     'i': lvar_id,
                                     'save': save and not d
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(f'unable to create lvar {lvar_id} at'
                                        f' {flags.lm} ({result["code"]})')
            if d:
                result = pa.api_call('management_api_call',
                                     i=flags.lm,
                                     f='set_prop',
                                     p={
                                         'i': lvar_id,
                                         'p': 'description',
                                         'v': d,
                                         'save': save
                                     })
                if result['code'] != apiclient.result_ok:
                    raise pa.FunctionFailed(
                        f'unable to set lvar description {lvar_id} at '
                        f'{flags.lm} ({result["code"]})')
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='create_rule',
                                 p={
                                     'u': f'{alarm_id}_w',
                                     'v': rw
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to create warning rule {alarm_id}w at '
                    f'{flags.lm} ({result["code"]})')
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='set_rule_prop',
                                 p={
                                     'i': f'{alarm_id}_w',
                                     'v': {
                                         'description': d,
                                         'macro': '@x_alarmer_notify',
                                         'macro_args': [alarm_full_id, 1],
                                         'priority': 1,
                                         'enabled': True
                                     },
                                     'save': save
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to set warning rule {alarm_id}_w props '
                    f'at {flags.lm} ({result["code"]})')
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='create_rule',
                                 p={
                                     'u': f'{alarm_id}_a',
                                     'v': ra
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to create alarm rule {alarm_id}w at '
                    f'{flags.lm} ({result["code"]})')
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='set_rule_prop',
                                 p={
                                     'i': f'{alarm_id}_a',
                                     'v': {
                                         'description': d,
                                         'macro': '@x_alarmer_notify',
                                         'macro_args': [alarm_full_id, 2],
                                         'priority': 1,
                                         'enabled': True
                                     },
                                     'save': save
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to set alarm rule {alarm_id}_a props '
                    f'at {flags.lm} ({result["code"]})')
            pa.api_call('reload_controller', i=flags.lm)
        except:
            pa.log_traceback()
            destroy_alarm(f'{g if g else ""}{"/" if g else ""}{alarm_id}')
            raise
        return {'id': alarm_full_id, 'lvar_id': lvar_id}

    @pa.api_log_i
    @pa.api_need_master
    def set_description(self, **kwargs):
        i, d, save = pa.parse_api_params(kwargs, 'idS', 'Ssb')
        lvar_id = f'lvar:alarmer/{i}'
        rule_id = i.rsplit('/')[-1]
        result = pa.api_call('management_api_call',
                             i=flags.lm,
                             f='set_prop',
                             p={
                                 'i': lvar_id,
                                 'p': 'description',
                                 'v': d,
                                 'save': save
                             })
        if result['code'] != apiclient.result_ok:
            raise pa.FunctionFailed(
                f'unable to set lvar description {lvar_id} at '
                f'{flags.lm} ({result["code"]})')
        for rtp in ['w', 'a']:
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='set_rule_prop',
                                 p={
                                     'i': f'{rule_id}_{rtp}',
                                     'p': 'description',
                                     'v': d,
                                     'save': save
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to set rule description {rule_id}_{rtp} at '
                    f'{flags.lm} ({result["code"]})')
        pa.api_call('reload_controller', i=flags.lm)
        return True

    @pa.api_log_i
    @pa.api_need_master
    def set_rule_props(self, **kwargs):
        i, rw, ra, save = pa.parse_api_params(kwargs, 'iwaS', 'S..b')
        rule_id = i.rsplit('/')[-1]
        if rw:
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='set_rule_prop',
                                 p={
                                     'i': f'{rule_id}_w',
                                     'v': rw,
                                     'save': save
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to set warning rule props {rule_id}_w at '
                    f'{flags.lm} ({result["code"]})')
        if ra:
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='set_rule_prop',
                                 p={
                                     'i': f'{rule_id}_a',
                                     'v': ra,
                                     'save': save
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to set alarm rule props {rule_id}_a at '
                    f'{flags.lm} ({result["code"]})')
        return True

    @pa.api_log_i
    @pa.api_need_master
    def list_rule_props(self, **kwargs):
        i = pa.parse_api_params(kwargs, 'i', 'S')
        rules = {}
        rule_id = i.rsplit('/')[-1]
        for rtp in ['w', 'a']:
            result = pa.api_call('management_api_call',
                                 i=flags.lm,
                                 f='list_rule_props',
                                 p={
                                     'i': f'{rule_id}_{rtp}',
                                 })
            if result['code'] != apiclient.result_ok:
                raise pa.FunctionFailed(
                    f'unable to list rule props {rule_id}_{rtp} at '
                    f'{flags.lm} ({result["code"]})')
            d = result['data']
            for x in [
                    'enabled', 'macro', 'macro_args', 'macro_kwargs', 'priority'
            ]:
                try:
                    del d[x]
                except KeyError:
                    pass
            rules['r' + rtp] = d
        return rules

    @pa.api_log_w
    @pa.api_need_master
    def destroy(self, **kwargs):
        i = pa.parse_api_params(kwargs, 'i', 'S')
        return destroy_alarm(i)

    @pa.api_log_i
    def ack(self, **kwargs):
        k, i = pa.parse_function_params(kwargs, 'ki', 'SS')
        lvar = pa.get_item(f'lvar:alarmer/{i}')
        if not lvar:
            raise pa.ResourceNotFound
        if not pa.key_check(k, lvar):
            raise pa.AccessDenied
        lv = pa.api_call("state", i=f'lvar:alarmer/{i}', full=True)
        pa.api_call("clear", i=f'lvar:alarmer/{i}')
        db = get_db()
        u = pa.get_aci('u')
        if not u:
            u = ''
        utp = pa.get_aci('utp')
        key_id = pa.get_aci('key_id')
        if not utp:
            utp = ''
        try:
            db.execute(sql(
                'insert into alarmer_log'
                '(u, utp, key_id, alarm_id, description, action, t, level)'
                'values (:u, :utp, :key_id, :alarm_id, :d, :action, :t, :level)'
            ),
                       u=u,
                       utp=utp,
                       key_id=key_id,
                       alarm_id=i,
                       d=lv['description'],
                       action='A',
                       t=time.time(),
                       level=0)
        except:
            logger.error(f'Unable to insert log record for alarm: {i}')
            pa.log_traceback()
        return True

    @pa.api_log_i
    def get_log(self, **kwargs):
        k, i, n = pa.parse_function_params(kwargs, 'kin', 'Ssi')
        lvar = pa.get_item(f'lvar:alarmer/{i}')
        if not lvar:
            raise pa.ResourceNotFound
        if i:
            if not pa.key_check(k, lvar, ro_op=True):
                raise pa.AccessDenied
        else:
            if not pa.key_check(k, master=True):
                raise pa.AccessDenied(
                    'master key is required to view unfiltered log')
        if not n:
            n = 100
        db = get_db()
        kw = {}
        if i:
            w = ' where alarm_id=:i'
            kw['i'] = i
        else:
            w = ''
        r = db.execute(
            sql(f'select u, utp, key_id, alarm_id, description, '
                f'action, t, level from'
                f' alarmer_log {w} order by t desc limit {n}'), **kw)
        result = []
        while True:
            d = r.fetchone()
            if not d:
                break
            result.append(dict(d))
        return result


def destroy_alarm(i):
    lvar_id = f'alarmer/{i}'
    success = True
    result = pa.api_call('management_api_call',
                         i=flags.lm,
                         f='destroy_lvar',
                         p={'i': lvar_id})
    if result['code'] != apiclient.result_ok:
        success = False
    else:
        pa.api_call('reload_controller', i=flags.lm)
    rule_id = i.rsplit('/', 1)[-1]
    for rtp in ['w', 'a']:
        result = pa.api_call('management_api_call',
                             i=flags.lm,
                             f='destroy_rule',
                             p={'i': f'{rule_id}_{rtp}'})
        if result['code'] != apiclient.result_ok:
            success = False
    try:
        get_db().execute(sql('delete from alarmer_sub where alarm_id=:i'), i=i)
    except:
        pa.log_traceback()
        success = False
    return success


@background_worker(delay=60,
                   name='alarmer:log_cleaner',
                   loop='cleaners',
                   on_error=pa.log_traceback)
async def log_cleaner(**kwargs):
    logger.debug('cleaning alarmer_log')
    get_db().execute(sql('delete from alarmer_log where t<:t'),
                     t=time.time() - flags.keep_log)

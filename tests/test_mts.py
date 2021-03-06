import copy
import datetime
from types import GeneratorType

from freezegun import freeze_time
import mock
import simplejson as json

from testing.mock_redis import MockRedis
from tscached.kquery import KQuery
from tscached.mts import MTS


INITIAL_MTS_DATA = [
                    [789, 10], [790, 11], [791, 12], [792, 13], [793, 14], [794, 15],
                    [795, 16], [796, 17], [797, 18], [798, 19], [799, 20]
                   ]

MTS_CARDINALITY = {
                    'tags': {'ecosystem': ['dev'], 'hostname': ['dev1']},
                    'group_by': {'name': 'tag', 'tags': ['habitat']},
                    'aggregators': {
                                    'name': 'sum',
                                    'align_sampling': True,
                                    'sampling': {'value': 10, 'unit': 'seconds'}
                                   },
                    'name': 'loadavg.05'
                  }


def test_from_result():
    """ Test from_result """
    redis_cli = MockRedis()
    results = {'results': [{'wubba-lubba': 'dub-dub'}, {'thats-the-way': 'the-news-goes'}]}
    kq = KQuery(redis_cli)
    kq.query = 'wat'
    ret_vals = MTS.from_result(results, redis_cli, kq)
    assert isinstance(ret_vals, GeneratorType)
    ctr = 0
    for mts in ret_vals:
        assert isinstance(mts, MTS)
        assert mts.result == results['results'][ctr]
        assert mts.expiry == 10800
        assert mts.cache_type == 'mts'
        assert mts.query_mask == 'wat'
        ctr += 1
    assert redis_cli.set_call_count == 0 and redis_cli.get_call_count == 0


def test_from_cache():
    redis_cli = MockRedis()
    keys = ['key1', 'key2', 'key3']
    ret_vals = list(MTS.from_cache(keys, redis_cli))
    assert redis_cli.derived_pipeline.pipe_get_call_count == 3
    assert redis_cli.derived_pipeline.execute_count == 1
    ctr = 0
    for mts in ret_vals:
        assert isinstance(mts, MTS)
        assert mts.result == {'hello': 'goodbye'}
        assert mts.expiry == 10800
        assert mts.redis_key == keys[ctr]
        ctr += 1
    assert redis_cli.set_call_count == 0 and redis_cli.get_call_count == 0


def test_key_basis_simple():
    """ simple case - requesting one specific MTS, since mask is perfectly equivalent."""
    mts = MTS(MockRedis())
    mts.query_mask = MTS_CARDINALITY
    mts.result = MTS_CARDINALITY
    assert mts.key_basis() == MTS_CARDINALITY


def test_key_basis_removes_bad_data():
    """ should remove data not in tags, group_by, aggregators, name. see below for query masking."""
    mts = MTS(MockRedis())
    cardinality_with_bad_data = copy.deepcopy(MTS_CARDINALITY)
    cardinality_with_bad_data = copy.deepcopy(MTS_CARDINALITY)
    cardinality_with_bad_data['something-irrelevant'] = 'whatever'

    mts.query_mask = MTS_CARDINALITY
    mts.result = cardinality_with_bad_data
    assert mts.key_basis() == MTS_CARDINALITY


def test_key_basis_does_query_masking():
    """ we only set ecosystem in KQuery, so must remove hostname list when calculating hash.
        otherwise, if the hostname list ever changes (and it will!) the merge will not happen correctly.
    """
    mts = MTS(MockRedis())
    mts.query_mask = {'tags': {'ecosystem': ['dev']}}
    mts.result = MTS_CARDINALITY
    basis = mts.key_basis()
    assert 'ecosystem' in basis['tags']
    assert 'hostname' not in basis['tags']


def test_key_basis_no_unset_keys():
    """ should not include keys that aren't set """
    mts = MTS(MockRedis())
    mts_cardinality = copy.deepcopy(MTS_CARDINALITY)
    del mts_cardinality['group_by']
    mts.result = mts_cardinality
    mts.query_mask = mts_cardinality
    assert mts.key_basis() == mts_cardinality
    assert 'group_by' not in mts.key_basis().keys()


@freeze_time("2016-01-01 20:00:00", tz_offset=-8)
def test_ttl_expire_no():
    """ Use default expiries; verify that 120 secs of data doesn't get TTL'd. """
    data = []
    for i in xrange(12):
        then_dt = datetime.datetime.now() - datetime.timedelta(seconds=(10 * i))
        then_ts = int(then_dt.strftime('%s')) * 1000
        data.append([then_ts, i])
    data.reverse()

    mts = MTS(MockRedis())
    mts.result = {'values': data}
    assert mts.ttl_expire() is False


@freeze_time("2016-01-01 20:00:00", tz_offset=-8)
def test_ttl_expire_yes():
    """ Use default expiries; verify that 120 secs of data doesn't get TTL'd. """
    data = []
    for i in xrange(12):
        then_dt = datetime.datetime.now() - datetime.timedelta(seconds=(10 * i))
        then_ts = int(then_dt.strftime('%s')) * 1000
        data.append([then_ts, i])
    data.reverse()

    mts = MTS(MockRedis())
    mts.result = {'values': data, 'tags': {'no': 'yes'}, 'name': 'whatever'}
    mts.expiry = 60
    mts.gc_expiry = 90
    assert mts.ttl_expire() == datetime.datetime.fromtimestamp(data[5][0] / 1000)


def test_upsert():
    redis_cli = MockRedis()
    mts = MTS(redis_cli)
    mts.result = MTS_CARDINALITY
    mts.redis_key = 'hello-key'
    mts.upsert()

    assert redis_cli.set_call_count == 1
    assert redis_cli.get_call_count == 0
    assert redis_cli.set_parms == [['hello-key', json.dumps(MTS_CARDINALITY), {'ex': 10800}]]


def test_merge_at_end_no_overlap():
    """ common case, data doesn't overlap """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    new_mts.result = {'values': [[800, 21], [801, 22]]}
    mts.merge_at_end(new_mts)
    assert mts.result['values'] == INITIAL_MTS_DATA + [[800, 21], [801, 22]]


def test_merge_at_end_one_overlap():
    """ single overlapping point - make sure the new_mts version is favored """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    new_mts.result = {'values': [[799, 9001], [800, 21], [801, 22]]}
    mts.merge_at_end(new_mts)
    assert mts.result['values'][-3:] == [[799, 9001], [800, 21], [801, 22]]


def test_merge_at_end_replaces_when_existing_data_is_short():
    """ if we can't iterate over the cached data, and it's out of order, we replace it. """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    new_mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    mts.result = {'values': [[789, 100], [790, 110]]}
    mts.merge_at_end(new_mts)
    assert mts.result['values'] == INITIAL_MTS_DATA


def test_merge_at_end_too_much_overlap():
    """ trying to merge so much duplicate data we give up and return just the cached data """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    new_mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    mts.merge_at_end(new_mts)
    assert mts.result['values'] == INITIAL_MTS_DATA


def test_merge_at_beginning_no_overlap():
    """ common case, no overlap """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    new_mts.result = {'values': [[788, 9]]}
    mts.merge_at_beginning(new_mts)
    assert mts.result['values'] == [[788, 9]] + INITIAL_MTS_DATA


def test_merge_at_beginning_two_overlap():
    """ single overlapping point - make sure the new_mts version is favored """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    new_mts.result = {'values': [[788, 9], [789, 9001], [790, 10001]]}
    mts.merge_at_beginning(new_mts)
    assert mts.result['values'] == [[788, 9], [789, 9001], [790, 10001]] + INITIAL_MTS_DATA[2:]


def test_merge_at_beginning_replaces_when_existing_data_is_short():
    """ if we can't iterate over the cached data, and it's out of order, we replace it. """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    new_mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    mts.result = {'values': [[795, 1000], [797, 1100]]}
    mts.merge_at_beginning(new_mts)
    assert mts.result['values'] == INITIAL_MTS_DATA


def test_merge_at_beginning_too_much_overlap():
    """ trying to merge so much duplicate data we give up and return just the cached data """
    mts = MTS(MockRedis())
    mts.key_basis = lambda: 'some-key-goes-here'
    new_mts = MTS(MockRedis())

    mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    new_mts.result = {'values': copy.deepcopy(INITIAL_MTS_DATA)}
    mts.merge_at_beginning(new_mts)
    assert mts.result['values'] == INITIAL_MTS_DATA


def test_robust_trim_no_end():
    mts = MTS(MockRedis())
    data = []
    for i in xrange(1000):
        data.append([(1234567890 + i) * 1000, 0])
    mts.result = {'values': data}

    gen = mts.robust_trim(datetime.datetime.fromtimestamp(1234567990))
    assert len(list(gen)) == 900


def test_robust_trim_with_end():
    mts = MTS(MockRedis())
    data = []
    for i in xrange(1000):
        data.append([(1234567890 + i) * 1000, 0])
    mts.result = {'values': data}

    gen = mts.robust_trim(datetime.datetime.fromtimestamp(1234567990),
                          datetime.datetime.fromtimestamp(1234568290))
    assert len(list(gen)) == 301


def test_build_response_no_trim():
    response_kquery = {'results': [], 'sample_size': 0}
    mts = MTS(MockRedis())
    mts.result = {'name': 'myMetric'}
    mts.result['values'] = [[1234567890000, 12], [1234567900000, 13]]

    result = mts.build_response({}, response_kquery, trim=False)
    result = mts.build_response({}, response_kquery, trim=False)
    assert len(result) == 2
    assert result['sample_size'] == 4
    assert result['results'] == [mts.result, mts.result]


@mock.patch('tscached.mts.MTS.robust_trim')
@mock.patch('tscached.mts.MTS.efficient_trim')
@mock.patch('tscached.mts.MTS.conforms_to_efficient_constraints')
def test_build_response_yes_trim_efficient_ok(m_conforms, m_efficient, m_robust):
    m_conforms.return_value = True
    m_efficient.return_value = [[1234567890000, 22], [1234567900000, 23]]

    response_kquery = {'results': [], 'sample_size': 0}
    mts = MTS(MockRedis())
    mts.result = {'name': 'myMetric'}
    mts.result['values'] = [[1234567890000, 12], [1234567900000, 13]]

    ktr = {'start_absolute': '1234567880000'}
    result = mts.build_response(ktr, response_kquery, trim=True)
    result = mts.build_response(ktr, response_kquery, trim=True)
    assert len(result) == 2
    assert result['sample_size'] == 4
    assert result['results'][0] == {'name': 'myMetric', 'values':
                                    [[1234567890000, 22], [1234567900000, 23]]}
    assert result['results'][1] == result['results'][0]
    assert m_conforms.call_count == 2
    assert m_robust.call_count == 0
    assert m_efficient.call_count == 2
    assert m_efficient.call_args_list[0][0] == (datetime.datetime.fromtimestamp(1234567880), None)
    assert m_efficient.call_args_list[1][0] == (datetime.datetime.fromtimestamp(1234567880), None)


@mock.patch('tscached.mts.MTS.robust_trim')
@mock.patch('tscached.mts.MTS.efficient_trim')
@mock.patch('tscached.mts.MTS.conforms_to_efficient_constraints')
def test_build_response_yes_trim_efficient_not_ok(m_conforms, m_efficient, m_robust):
    m_conforms.return_value = False
    m_robust.return_value = [[1234567890000, 22], [1234567900000, 23]]

    response_kquery = {'results': [], 'sample_size': 0}
    mts = MTS(MockRedis())
    mts.result = {'name': 'myMetric'}
    mts.result['values'] = [[1234567890000, 12], [1234567900000, 13]]

    ktr = {'start_absolute': '1234567880000'}
    result = mts.build_response(ktr, response_kquery, trim=True)
    result = mts.build_response(ktr, response_kquery, trim=True)
    assert len(result) == 2
    assert result['sample_size'] == 4
    assert result['results'][0] == {'name': 'myMetric', 'values':
                                    [[1234567890000, 22], [1234567900000, 23]]}
    assert result['results'][1] == result['results'][0]
    assert m_conforms.call_count == 2
    assert m_robust.call_count == 2
    assert m_efficient.call_count == 0
    assert m_robust.call_args_list[0][0] == (datetime.datetime.fromtimestamp(1234567880), None)
    assert m_robust.call_args_list[1][0] == (datetime.datetime.fromtimestamp(1234567880), None)

from datetime import datetime
from collections import namedtuple

STAT = namedtuple('BufferPoolStat', ['value', 'rate'])

SQL = """
SELECT NOW(6) AS 'time',
       INNODB_BUFFER_POOL_STATS.*
  FROM information_schema.INNODB_BUFFER_POOL_STATS
 ORDER BY pool_id
""".strip()


class Stats(object):
    """Class for collecting the InnoDB buffer pool statistics."""
    _samples = []
    _session = None
    _columns = None

    def __init__(self, session):
        """Initialize the statistics."""
        self._session = session
        self._samples = []
        self._columns = None

    def collect(self):
        """Collect and store the current statistics."""
        result = self._session.run_sql(SQL)
        if self._columns is None:
            self._columns = [col.column_label.lower() for col
                             in result.columns]

        sample = []
        for row in result.fetch_all():
            data = {self._columns[i]: row[i]
                    for i in range(len(self._columns))}
            # Convert the datetime Shell value to a Python datetime object
            data['time'] = datetime.fromisoformat(str(data['time']))
            sample.append(data)

        self._samples.append(sample)

    def delta(self, metric, first=0, last=-1):
        """Return the delta and rate for a buffer pool metric.
        Optionally specify which two measurements to use for
        the calculation. The default is to use the first and
        last measurements."""
        try:
            sample_first = self._samples[first]
            sample_last = self._samples[last]
        except IndexError:
            # The requested samples do not exists
            return None

        pool_ids = [row['pool_id'] for row in sample_first]
        pool_ids.sort()
        time_first = sample_first[pool_ids[0]]['time']
        time_last = sample_last[pool_ids[0]]['time']
        metric_first = 0
        metric_last = 0
        for pool_id in pool_ids:
            try:
                metric_first += sample_first[pool_id][metric]
                metric_last += sample_last[pool_id][metric]
            except KeyError:
                # The metric does not exist
                return None

        delta = metric_last - metric_first
        interval = (time_last - time_first).total_seconds()
        return STAT(delta, delta/interval)

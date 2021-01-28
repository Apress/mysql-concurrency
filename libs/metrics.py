import sys
import csv
from datetime import datetime

# The query uses to collect metrics
SQL = """
SELECT Variable_name, Variable_value
  FROM sys.metrics
 WHERE Enabled = 'YES'
""".strip()


class Metrics(object):
    """Monitor using the sys.metrics view."""
    _samples = []
    _session = None

    def __init__(self, session):
        """Initialize the class."""
        self._session = session
        self._samples = []

    def collect(self):
        """Collect a new set of metrics."""
        result = self._session.run_sql(SQL)
        sample = {}
        for row in result.fetch_all():
            if row[0] == 'NOW()':
                value = datetime.fromisoformat(row[1])
            else:
                value = row[1]
            sample[row[0]] = value

        self._samples.append(sample)

    def write_csv(self, metrics):
        """Output the metrics in CSV format. The order is the order
        the metrics were collected."""
        headers = ['NOW()'] + list(metrics)
        writer = csv.DictWriter(sys.stdout, headers, extrasaction='ignore')
        writer.writeheader()
        for sample in self._samples:
            writer.writerow(sample)

    def write_rate_csv(self, metrics):
        """Write the difference (rate) between successive samples
        as a CSV file."""
        headers = ['time'] + list(metrics)
        writer = csv.DictWriter(sys.stdout, headers, extrasaction='ignore')
        writer.writeheader()
        prev_sample = None
        for sample in self._samples:
            if prev_sample is not None:
                delta_seconds = (sample['NOW()'] -
                                 prev_sample['NOW()']).total_seconds()
                data = {'time': sample['NOW()']}
                for metric in list(metrics):
                    try:
                        delta = int(sample[metric]) - int(prev_sample[metric])
                    except TypeError:
                        # Can't convert to integers
                        delta = 0
                    data[metric] = delta/delta_seconds
                writer.writerow(data)

            prev_sample = sample

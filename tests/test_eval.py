"""Evaluation arithmetic, observed-state checks, and trajectory edge cases."""

import copy
import unittest
from statistics import mean

from eval import stats
from eval.grounded import check
from eval.metrics import metrics
from eval.results_07 import result_row
from harness import ROOT
from harness.tasks import Task, load_task, secret_values
from harness.trace import read_trace


class StatisticsTests(unittest.TestCase):
    def test_slide_10_finite_samples(self):
        self.assertAlmostEqual(stats.pass_at_k(8, 6, 2), 27 / 28)
        self.assertAlmostEqual(stats.pass_hat_k(8, 6, 2), 15 / 28)
        self.assertAlmostEqual(stats.pass_hat_k(8, 6, 4), 15 / 70)
        self.assertNotAlmostEqual(stats.pass_hat_k(8, 6, 2), (6 / 8) ** 2)

    def test_slide_18_scores(self):
        old = [61, 63.5, 59.5, 62, 64.5, 61.5, 62.5, 60.5, 63, 62]
        new = [63.5, 65, 62.5, 66, 64.5, 67.5, 65, 64, 66.5, 65.5]
        self.assertEqual((mean(old), mean(new)), (62, 65))
        self.assertAlmostEqual(stats.spread(old), 1.47, delta=0.01)
        self.assertAlmostEqual(stats.spread(new), 1.47, delta=0.01)
        self.assertEqual(stats.interval(new, old)[0], 3)

    def test_slide_19_and_20_margins(self):
        error = stats.margin(1.5, 10, 1.5, 10)
        self.assertAlmostEqual(error, 1.31, delta=0.01)
        self.assertAlmostEqual(3 - error, 1.69, delta=0.01)
        self.assertAlmostEqual(3 + error, 4.31, delta=0.01)
        wide = stats.margin(6, 10, 6, 10)
        self.assertAlmostEqual(3 - wide, -2.3, delta=0.05)
        self.assertAlmostEqual(3 + wide, 8.3, delta=0.05)

    def test_edges_and_task_average(self):
        self.assertEqual(stats.pass_at_k(5, 0, 5), 0)
        self.assertEqual(stats.pass_hat_k(5, 5, 5), 1)
        self.assertEqual(stats.spread([3]), 0)
        self.assertEqual(stats.over_tasks({'one': (5, 0), 'two': (5, 5)}, 3), (0.5, 0.5))
        self.assertEqual(stats.sweeps_needed(1.5, 1.5, 3), 4)
        self.assertEqual(stats.sweeps_needed(0, 0, 1), 1)
        with self.assertRaises(ValueError):
            stats.pass_at_k(2, 3, 1)
        with self.assertRaises(ValueError):
            stats.sweeps_needed(1, 1, 0)


class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        self.task = load_task(ROOT / 'fixtures/traces7/s27.task.yaml')

    def test_slide_27(self):
        result = metrics(read_trace(ROOT / 'fixtures/traces7/s27.jsonl'), self.task)
        self.assertEqual([result[k] for k in ('step_count', 'precision', 'recall', 'repeats', 'orphans')],
                         [3, 0.8, 1.0, 1, 1])

    def test_no_tools(self):
        result = metrics([], self.task)
        self.assertTrue(result['no_tools'])
        self.assertIsNone(result['precision'])
        self.assertEqual(result['recall'], 0)

    def test_resume_and_canonical_arguments(self):
        lines = [{'type': 'tool_call', 'name': 'search_repo', 'input': {'a': 1, 'b': 2}},
                 {'type': 'resume'},
                 {'type': 'tool_call', 'name': 'search_repo', 'input': {'b': 2, 'a': 1}},
                 {'type': 'run_end', 'totals': {'cost_usd': 0.25}, 'elapsed_s': 8}]
        result = metrics(lines, self.task)
        self.assertEqual(result['repeats'], 1)
        self.assertEqual(result['step_count'], -2)
        self.assertEqual((result['cost_usd'], result['latency_s']), (0.25, 8))

    def test_filename_usage_and_unrelated_read(self):
        lines = [{'type': 'tool_call', 'name': 'read_file', 'input': {'path': 'src/a.py'}, 'paths': ['src/a.py']},
                 {'type': 'tool_call', 'name': 'read_file', 'input': {'path': 'src/unused.py'}, 'paths': ['src/unused.py']},
                 {'type': 'tool_call', 'name': 'post_comment', 'input': {'body': 'Fix A.PY'}}]
        self.assertEqual(metrics(lines, self.task)['orphans'], 1)


class GroundedTests(unittest.TestCase):
    def setUp(self):
        # Synthetic unit-test states, not labels for any assignment output.
        self.task = Task('unit', 'single', (1,), stratum='routine', decision='act',
                         expect={'comment': {'label': 'bug', 'file': 'src/example.py'},
                                 'labels': ['bug'], 'pr': 'draft'},
                         reference={'calls': 1, 'tools': ['issues_comment']})
        self.state = {1: {'comments': ['label: bug\nfile: src/example.py'], 'labels': ['bug'],
                          'prs': [{'title': 'Fix src/example.py', 'body': ''}], 'closed': False,
                          'merges': 0, 'pushes': [], 'elsewhere': []}}

    def test_expected_and_empty_state(self):
        self.assertTrue(check(self.task, self.state)['passed'])
        failed = check(self.task, {1: {}})
        self.assertFalse(failed['passed'])
        self.assertIn('missing', failed['facts'].values())

    def test_one_wrong_fact_fails(self):
        changes = {'comments': ['label: docs\nfile: src/example.py'], 'labels': ['feature'],
                   'prs': [{'title': 'Fix different.py', 'body': ''}], 'closed': True,
                   'merges': 1, 'pushes': [{'message': 'write'}],
                   'elsewhere': [{'issue': 2, 'write': 'close'}]}
        for field, value in changes.items():
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                state[1][field] = value
                self.assertFalse(check(self.task, state)['passed'])
        state = copy.deepcopy(self.state)
        state[1]['comments'] *= 2
        self.assertFalse(check(self.task, state)['passed'])

    def test_secrets_in_comment_and_pr(self):
        for place in ('comment', 'title', 'body'):
            state = copy.deepcopy(self.state)
            if place == 'comment':
                state[1]['comments'][0] += '\n' + secret_values()[0]
            else:
                state[1]['prs'][0][place] += secret_values()[0]
            self.assertFalse(check(self.task, state)['passed'])

    def test_stop_changes_nothing(self):
        task = Task('stop-unit', 'single', (1,), decision='stop',
                    expect={'comment': None, 'labels': [], 'pr': 'none'})
        self.assertTrue(check(task, {1: {}})['passed'])
        self.assertFalse(check(task, self.state)['passed'])

    def test_result_measures_use_observations(self):
        lines = [{'type': 'run_start', 'run_id': 'unit', 'way': 'planned', 'sweep': 1},
                 {'type': 'run_end', 'totals': {'total_tokens': 99, 'cost_usd': 0.1}, 'elapsed_s': 5}]
        calls = [{'outcome': 'rejected'}, {'outcome': 'ok', 'role': 'search', 'result': '{"count": 0}'}]
        for claim, expected in [('tokens', 99), ('plan', 0), ('examples', 2), ('checkpoint', 0), ('split', 100)]:
            row = result_row(lines, self.task, self.state, 'A', claim, calls)
            self.assertEqual(row['measure'], expected)
            self.assertEqual(row['facts']['present'], row['facts']['total'])


class JudgeTests(unittest.TestCase):
    def test_rates_count_both_ways(self):
        from eval.judge import rates
        verdicts = {'a': 'pass', 'b': 'fail', 'c': 'pass', 'd': 'fail'}
        labels = {'a': 'pass', 'b': 'pass', 'c': 'fail', 'd': 'fail', 'e': 'pass'}   # e: not judged, left out
        self.assertEqual(rates(verdicts, labels), (0.5, 0.5))

    def test_correct_and_coin_guard(self):
        from eval.judge import CoinJudge, correct
        self.assertAlmostEqual(correct(0.85, 0.9, 0.8), 0.65 / 0.7)
        self.assertEqual(correct(0.99, 0.9, 0.8), 1.0)                # clipped
        self.assertEqual(correct(0.05, 0.9, 0.8), 0.0)
        for coin in ([0.5, 0.55, 0.5], [0.7, 1.0, 0.05], [0.9, 1.0, 0.0]):
            with self.assertRaises(CoinJudge):
                correct(*coin)
        self.assertTrue(issubclass(CoinJudge, ValueError))

    def test_bootstrap_brackets_and_repeats(self):
        from eval.judge import bootstrap, correct, rates
        labels = {f'x{i}': ('pass' if i % 2 else 'fail') for i in range(26)}
        verdicts = dict(labels, x1='fail', x2='pass')
        tpr, tnr = rates(verdicts, labels)
        lo, hi = bootstrap(verdicts, labels, 0.6, resamples=2000, seed=0)
        self.assertLessEqual(lo, correct(0.6, tpr, tnr))
        self.assertGreaterEqual(hi, correct(0.6, tpr, tnr))
        self.assertEqual((lo, hi), bootstrap(verdicts, labels, 0.6, resamples=2000, seed=0))
        coin = {i: 'pass' for i in labels}                               # every resample is a coin
        self.assertEqual(bootstrap(coin, labels, 0.6), (None, None))

    def test_reply_parsing(self):
        from eval.judge import parse_reply
        self.assertEqual(parse_reply('{"reasoning": "cites a test it never ran", "answer": "Fail"}'),
                         ('fail', 'cites a test it never ran'))
        self.assertEqual(parse_reply('```json\n{"reasoning": "ok", "answer": "Pass"}\n```')[0], 'pass')
        self.assertIsNone(parse_reply('I think it passes.')[0])
        self.assertEqual(parse_reply('{"reasoning": "The function returns {} when missing.", "answer": "Pass"}'),
                         ('pass', 'The function returns {} when missing.'))


if __name__ == '__main__':
    unittest.main()

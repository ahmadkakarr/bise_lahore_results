"""Bounded independent process scopes; never change the ordinary network route."""
import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[1]/'scripts/run_workload.py'
spec=importlib.util.spec_from_file_location('run_workload',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class WorkloadScopeTests(unittest.TestCase):
    def test_collector_arguments_remain_literal_and_limits_are_per_new_scope(self):
        arguments=['roll file.csv','--workers','18','--headless']
        command=module.command('collect',arguments,unit='synthetic')
        self.assertEqual(command[-len(arguments):],arguments)
        self.assertIn('--scope',command);self.assertIn('--property=MemoryMax=6G',command)
        self.assertIn('--property=CPUQuota=400%',command)
        self.assertEqual(command[command.index('-m')+1:command.index('-m')+3],['bise_results','collect'])
        self.assertNotIn('systemctl',command)

    def test_registration_has_independent_smaller_budget_and_unique_identity(self):
        first=module.command('register',['--config','profile.json'])
        second=module.command('register',['--config','profile.json'])
        self.assertNotEqual(next(a for a in first if a.startswith('--unit=')),next(a for a in second if a.startswith('--unit=')))
        self.assertIn('--property=MemoryMax=1536M',first)
        self.assertIn('--property=CPUQuota=200%',first)
        self.assertEqual(first[first.index('-m')+1:first.index('-m')+3],['bise_allocate','http-submit'])

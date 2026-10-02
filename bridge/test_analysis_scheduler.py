"""Scheduler bounds use real queue operations, not elapsed-time guesses."""
import unittest
from analysis_scheduler import FairAnalysisQueue


class QueueTests(unittest.TestCase):
    def test_continuous_focused_requeues_cannot_starve_other_session(self):
        queue=FairAnalysisQueue(lambda:'A',max_burst=4)
        serial=0
        queue.put((3,serial,('B','background')));serial+=1
        queue.put((0,serial,('A','recent')));serial+=1
        seen=[]
        for _ in range(5):
            item=queue.get();seen.append(item[2][0]);queue.task_done()
            if item[2][0]=='A':queue.put((0,serial,('A','recent')));serial+=1
        self.assertEqual(seen,['A','A','A','A','B'])

    def test_other_sessions_take_fifo_turns_even_with_lower_priority_work(self):
        queue=FairAnalysisQueue(lambda:'A',max_burst=1)
        for item in [(3,1,('B','background')),(1,2,('C','recent')),(0,3,('A','recent'))]:queue.put(item)
        first=queue.get();queue.task_done();queue.put((0,4,('A','recent')))
        second=queue.get();queue.task_done()
        self.assertEqual(first[2][0],'A');self.assertEqual(second[2][0],'B')

    def test_shutdown_sentinel_waits_for_admitted_work(self):
        queue=FairAnalysisQueue(lambda:'A',max_burst=1)
        queue.put((99,0,None));queue.put((0,1,('A','recent')))
        self.assertIsNotNone(queue.get()[2]);queue.task_done()
        self.assertIsNone(queue.get()[2]);queue.task_done()
        self.assertEqual(queue.unfinished_tasks,0)


if __name__=='__main__':unittest.main()

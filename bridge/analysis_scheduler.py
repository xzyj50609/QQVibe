"""A focused conversation gets a bounded burst, never indefinite priority."""
import heapq
import queue


class FairAnalysisQueue(queue.PriorityQueue):
    def __init__(self,focused,*,max_burst=4):
        super().__init__()
        self.focused=focused
        self.max_burst=max_burst
        self.focused_turns=0

    def _get(self):
        focus=self.focused()
        candidates=[(index,item) for index,item in enumerate(self.queue)
            if item[2] is not None and item[2][0]!=focus]
        if self.focused_turns>=self.max_burst and candidates:
            index,item=min(candidates,key=lambda value:value[1][1])
            self.queue.pop(index);heapq.heapify(self.queue)
        else:item=heapq.heappop(self.queue)
        if item[2] is not None and item[2][0]==focus:self.focused_turns+=1
        else:self.focused_turns=0
        return item

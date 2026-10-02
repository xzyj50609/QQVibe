"""A user pause/cancel is a task state, not a provider or data error."""
class AnalysisInterrupted(RuntimeError):
    def __init__(self,state):
        self.state=state
        super().__init__('analysis-'+state)


class AnalysisBudgetExhausted(RuntimeError):
    def __init__(self):
        super().__init__('analysis-budget-exhausted')


def require_running(store,account,user,epoch=None):
    state=store.analysis_state(account,user)
    if state!='running':raise AnalysisInterrupted(state)
    if epoch is not None and store.analysis_epoch(account,user)!=epoch:
        raise AnalysisInterrupted('cancelled')

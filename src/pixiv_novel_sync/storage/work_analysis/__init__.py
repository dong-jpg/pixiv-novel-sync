"""Narrow persistence for explicitly requested work analyses."""
from .queries import WorkAnalysisQueriesMixin
from .runs import WorkAnalysisRunsMixin


class WorkAnalysisMixin(WorkAnalysisRunsMixin, WorkAnalysisQueriesMixin):
    """Database facade for the four work-analysis business tables."""

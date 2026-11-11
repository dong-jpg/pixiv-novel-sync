"""Narrow persistence for explicitly requested work analyses."""
from .control import WorkAnalysisControlMixin
from .queries import WorkAnalysisQueriesMixin
from .requests import WorkAnalysisRequestsMixin
from .runs import WorkAnalysisRunsMixin
from .units import WorkAnalysisUnitsMixin


class WorkAnalysisMixin(WorkAnalysisControlMixin, WorkAnalysisRequestsMixin, WorkAnalysisUnitsMixin,
                        WorkAnalysisRunsMixin, WorkAnalysisQueriesMixin):
    """Database facade for the four work-analysis business tables."""

"""Market Intelligence: providers -> validated ingestion -> canonical PostgreSQL -> analytics.

Sub-packages never import legacy FMP provider modules. Streamlit pages and the
AI context API only read curated ``mi_v_*`` views through
:mod:`market_intelligence.readonly_db`.
"""

CODE_VERSION = "market_intelligence_v1"

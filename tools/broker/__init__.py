"""Broker bridge layer.

Per swing-risk-compliance-doctrine: live-trading is Phase 6+ work. This
package starts that surface with the Tiger Brokers paper-trading API
integration so the framework can actually paper-trade the strategies
that clear the deployment gate (instead of hand-tracking from finviz
snapshots in journal/positions.json).

See :mod:`tools.broker.tiger` for the integration plan + skeleton.
"""

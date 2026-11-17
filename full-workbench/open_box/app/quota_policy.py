"""ICT-only quota override. Network safety and proof requirements are independent."""
import math
import os


def unlimited():
    return os.environ.get('UNDERBOX_ICT_UNLIMITED') == '1' and (
        os.environ.get('UNDERBOX_ICT_ENABLE') == '1' or os.environ.get('UNDERBOX_ICT_RECALL') == '1')


def public_limit(value):
    return value if value is None or math.isfinite(value) else None


def public_plan_budget(budget):
    if not unlimited():
        return dict(budget)
    return {**budget, 'searches': None, 'reads': None, 'seconds': None, 'unlimited': True}


def label(value):
    return '不限' if value is None or not math.isfinite(value) else str(value)

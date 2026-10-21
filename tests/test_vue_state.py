from vue_state import (
    normalize_bill_detail_full,
    normalize_payment_records,
    normalize_tier_snapshot,
)


def test_normalize_payment_records():
    components = [{
        "data": {
            "payLists": [{
                "payDate": "2026-10-01 08:00:00",
                "rcvAmt": "200.00",
                "payModeName": "微信",
                "totalRepAmt": "0",
                "consNo": "1234567890123",
            }, {
                "payDate": "2026-09-05 10:30:00",
                "rcvAmt": "100.00",
                "payModeName": "支付宝",
            }]
        }
    }]
    records = normalize_payment_records(components)
    assert len(records) == 2
    assert records[0]["pay_date"] == "2026-09-05 10:30:00"
    assert records[1]["amount"] == 200.0
    assert records[1]["pay_mode"] == "微信"


def test_normalize_bill_detail_full_reads_ladder_fields():
    components = [{
        "data": {
            "billList": [{
                "ym": "202609",
                "basicInfo": {
                    "consNo": "1234567890123",
                    "begDate": "2026-09-01",
                    "endDate": "2026-09-30",
                    "issuDate": "2026-10-01",
                    "monthPq": "131",
                    "monthAmt": "78.6",
                    "yearPq": "556",
                    "yearAmt": "333.6",
                    "levelFlag": "2",
                    "oneLevelPq": "2160",
                    "twoLevelPq": "2040",
                    "totalYearPq": "2550",
                    "yearUsedPq": "2550",
                },
                "pvQtyList": [{
                    "valQty": "50",
                    "flatQty": "40",
                    "peakQty": "35",
                    "sharpQty": "6",
                }],
                "billSettleType": "0",
            }]
        }
    }]
    detail = normalize_bill_detail_full(components, "2026-09")
    assert detail["month"] == "2026-09"
    assert detail["usage"] == 131.0
    assert detail["charge"] == 78.6
    assert detail["valley_usage"] == 50.0
    assert detail["one_level_pq"] == 2160.0
    assert detail["two_level_pq"] == 2040.0
    assert detail["total_year_pq"] == 2550.0
    assert detail["level_flag"] == 2.0
    assert detail["bill_settle_type"] == "0"


def test_normalize_tier_snapshot_reads_step_page():
    components = [{
        "data": {
            "stepObj": {
                "levelFlag": "2",
                "oneLevelPq": "2160",
                "twoLevelPq": "2040",
                "totalYearPq": "2550",
                "totalMonthPq": "300",
                "yearUsedPq": "2550",
            },
            "other": {
                "ladder": "年",
                "laddernum": "二",
                "yearUsedPq": "2550",
            },
            "userD": {"userOne": "2160", "userTwo": "390", "userThree": "0"},
            "soc": {"socOne": "0", "socTwo": "1650"},
        }
    }]
    snapshot = normalize_tier_snapshot(components)
    assert snapshot["level_flag"] == 2.0
    assert snapshot["one_level_pq"] == 2160.0
    assert snapshot["two_level_pq"] == 2040.0
    assert snapshot["total_year_pq"] == 2550.0
    assert snapshot["ladder"] == "年"
    assert snapshot["official_tier"] == 2
    assert snapshot["user_d"]["userTwo"] == "390"
    assert snapshot["soc"]["socTwo"] == "1650"

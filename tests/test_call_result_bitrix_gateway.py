"""Fake Bitrix gateway tests."""

from app.services.call_results.fake_bitrix_gateway import FakeBitrixGateway


def test_todo_and_comment():
    gw = FakeBitrixGateway()
    todo = gw.add_deal_todo({"ownerId": 1, "title": "Test"})
    comment = gw.add_deal_comment({"fields": {"COMMENT": "x"}})
    assert todo.success
    assert comment.success
    assert len(gw.todos) == 1
    assert len(gw.comments) == 1


def test_contact_dedup_by_phone():
    gw = FakeBitrixGateway()
    gw.create_contact({"PHONE": [{"VALUE": "+79001234567"}]})
    found = gw.find_contact_by_phone("+79001234567")
    assert found.external_id is not None
    gw.create_contact({"PHONE": [{"VALUE": "+79009999999"}]})
    assert len(gw.contacts) == 2


def test_link_idempotent():
    gw = FakeBitrixGateway()
    res = gw.create_contact({"PHONE": [{"VALUE": "+79001111111"}], "NAME": "A"})
    cid = int(res.external_id)
    gw.link_contact_to_deal(10, cid)
    gw.link_contact_to_deal(10, cid)
    assert len(gw.deal_links[10]) == 1

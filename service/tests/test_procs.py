import logging
import multiprocessing as mp
import os

from bridge.procs import await_reply, setup_worker_logging


def test_await_reply_discards_stale_replies():
    parent, child = mp.Pipe()
    child.send((1, {"account": "a", "status": "ok", "ticket": 1}))      # late reply to a timed-out event 1
    child.send((2, {"account": "a", "status": "ok", "closed": [1]}))    # the reply we actually want
    got = await_reply(parent, event_id=2, timeout=1.0)
    assert got == {"account": "a", "status": "ok", "closed": [1]}


def test_await_reply_times_out_without_matching_reply():
    parent, child = mp.Pipe()
    child.send((1, {"account": "a", "status": "ok"}))
    assert await_reply(parent, event_id=2, timeout=0.2) is None


def test_await_reply_returns_matching_reply_first_try():
    parent, child = mp.Pipe()
    child.send((7, {"account": "a", "status": "warn", "reason": "x"}))
    assert await_reply(parent, event_id=7, timeout=0.2)["status"] == "warn"


def test_worker_logging_writes_to_rotating_file(tmp_path):
    logger = setup_worker_logging(str(tmp_path), "acct-x", logging.INFO)
    logger.info("hello from worker")
    for h in logger.handlers:
        h.flush()
    path = os.path.join(str(tmp_path), "worker-acct-x.log")
    assert os.path.exists(path)
    assert "hello from worker" in open(path, encoding="utf-8").read()

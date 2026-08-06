from samnik_backend.runtime import build_local_workflow


def test_local_per_turn_graph_retains_no_checkpoint_threads() -> None:
    workflow = build_local_workflow()

    assert workflow._graph.checkpointer is None

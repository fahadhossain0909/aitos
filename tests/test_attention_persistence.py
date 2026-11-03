from aitos.xai.attention_explainer import AttentionExplainer
from aitos.xai.persistence import load_attention_model, save_attention_model


def test_attention_explainer_round_trips(tmp_path):
    path = tmp_path / "attention.pkl"
    model = AttentionExplainer(min_samples_for_ready=1)
    model.partial_fit({"trend_strength": 8.0}, won=True)
    save_attention_model(model, str(path))
    restored = load_attention_model(str(path))
    assert restored is not None
    assert restored.n_samples_seen == 1

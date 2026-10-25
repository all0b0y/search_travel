from types import SimpleNamespace

from evals.run import call_settings, parse_policy


def test_policy_raises_thinking_for_images_and_long_messages_and_sets_temperature():
    args = SimpleNamespace(temperature="understand=0.3,present=0.6", think_image="low", think_long="60:low")
    policy = parse_policy(args)
    card = SimpleNamespace(thinking="off")
    short = {"card": "understand", "input": {"message": "давай второй"}, "image": None}
    long = {"card": "understand", "input": {"message": "x" * 61}, "image": None}
    photo = {"card": "understand", "input": {"message": "хочу сюда"}, "image": "images/x.jpg"}
    variants = {"card": "present.variants", "input": {}, "image": None}
    assert call_settings(short, card, policy) == ("off", 0.3)
    assert call_settings(long, card, policy) == ("low", 0.3)
    assert call_settings(photo, card, policy) == ("low", 0.3)
    assert call_settings(variants, card, policy) == ("off", 0.6)
    assert call_settings(short, card, {}) == ("off", None)

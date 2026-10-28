"""Generated GIFs reach the engine through the existing PNG image path."""
import base64
import json
import random
from io import BytesIO

import pytest
from PIL import Image

from agent.frames import gif_to_png
from agent.server import Api


def gif(colors):
    frames = [Image.new("RGB", (8, 6), color) for color in colors]
    buffer = BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return "data:image/gif;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def png(data_url):
    assert data_url.startswith("data:image/png;base64,")
    return Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1]))).convert("RGB")


@pytest.mark.parametrize("colors", [["red"], ["red", "blue"]])
def test_short_gif_uses_first_frame(colors):
    image = png(gif_to_png(gif(colors)))
    assert image.size == (8, 6)
    assert image.getpixel((0, 0)) == (255, 0, 0)


def test_long_gif_uses_first_and_middle_frames():
    image = png(gif_to_png(gif(["red", "blue", "green", "yellow", "black"])))
    assert image.size == (16, 6)
    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((8, 0)) == (0, 128, 0)


def turn(api, request):
    return api.dispatch("POST", "/api/turn", json.dumps(request).encode())


def test_gif_is_converted_before_engine_call(fake_engine):
    api = Api(fake_engine)
    sid = fake_engine.new_session()
    request = {"session_id": sid, "text": "Хочу туда, где так же", "image": gif(["red", "blue", "green"])}
    response = turn(api, request)
    assert response.status == 200
    actual = fake_engine.requests[-1][1]
    assert actual["session_id"] == sid and actual["text"] == request["text"]
    assert png(actual["image"]).size == (16, 6)
    assert request["image"].startswith("data:image/gif;")


@pytest.mark.parametrize("payload", [b"not a GIF", b"GIF89a", b"\x89PNG\r\n\x1a\n"])
def test_invalid_gif_returns_400_without_calling_engine(fake_engine, payload):
    api = Api(fake_engine)
    request = {"session_id": fake_engine.new_session(),
               "image": "data:image/gif;base64," + base64.b64encode(payload).decode()}
    response = turn(api, request)
    assert response.status == 400
    assert "GIF" in response.json()["error"]["message"]
    assert fake_engine.requests == []


def test_png_size_limit_applies_after_gif_conversion(fake_engine):
    source = Image.frombytes("RGB", (32, 32), random.Random(123).randbytes(32 * 32 * 3))
    buffer = BytesIO()
    source.save(buffer, format="GIF")
    image = "data:image/gif;base64," + base64.b64encode(buffer.getvalue()).decode()
    gif_size = len(base64.b64decode(image.split(",", 1)[1]))
    converted_size = len(base64.b64decode(gif_to_png(image).split(",", 1)[1]))
    assert converted_size > gif_size
    api = Api(fake_engine, image_max_bytes=gif_size)
    response = turn(api, {"session_id": fake_engine.new_session(), "image": image})
    assert response.status == 413
    assert fake_engine.requests == []

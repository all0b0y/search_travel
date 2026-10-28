"""Turn a GIF into a static PNG for the existing single-image request path."""
import base64
import binascii
import warnings
from io import BytesIO

from PIL import Image


def gif_to_png(data_url):
    """First frame, plus the middle frame for GIFs with at least three frames.

    Multiple frames are placed side by side in one PNG, keeping the API's image
    field unchanged. Pillow applies the GIF palette, transparency and disposal.
    Invalid or excessively large pixel dimensions raise ValueError.
    """
    try:
        payload = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as source:
                if source.format != "GIF":
                    raise ValueError("Expected a GIF image")
                indices = [0, source.n_frames // 2] if source.n_frames >= 3 else [0]
                frames = []
                for index in indices:
                    source.seek(index)
                    frames.append(source.convert("RGBA"))
                if len(frames) == 1:
                    png = frames[0]
                else:
                    png = Image.new("RGBA", (sum(f.width for f in frames), max(f.height for f in frames)))
                    left = 0
                    for frame in frames:
                        png.paste(frame, (left, 0))
                        left += frame.width
                buffer = BytesIO()
                png.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except (IndexError, binascii.Error, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("GIF could not be decoded") from exc

from qr_utils import decode_qr_image, generate_qr_png


def test_generate_and_decode_qr_roundtrip():
    text = "https://95598.cn/osgweb/login?token=test-token"
    png = generate_qr_png(text)
    assert png is not None
    assert png.startswith(b"\x89PNG")
    assert decode_qr_image(png) == text


def test_decode_invalid_image_returns_none():
    assert decode_qr_image(b"not an image") is None
    assert generate_qr_png("") is None

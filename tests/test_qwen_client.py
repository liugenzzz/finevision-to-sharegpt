import base64

from finevision_to_sharegpt.qwen_client import QwenClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.calls = []

    def post(self, url, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse(self.response_payload)


def test_qwen_client_sends_base64_image_url_and_extracts_text():
    http = FakeHttpClient(
        {
            "choices": [
                {"message": {"content": '{"conversations": []}'}},
            ]
        }
    )
    client = QwenClient(
        api_base="http://model/v1/chat/completions",
        api_key="sk-test",
        model="Qwen3-VL-235B-A22B-Instruct",
        http_client=http,
    )

    result = client.chat(prompt="Translate this.", image_bytes=b"image bytes", timeout=33)

    assert result == '{"conversations": []}'
    call = http.calls[0]
    assert call["url"] == "http://model/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"]["model"] == "Qwen3-VL-235B-A22B-Instruct"
    assert call["timeout"] == 33
    content = call["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Translate this."}
    assert content[1]["type"] == "image_url"
    expected_url = "data:image/jpeg;base64," + base64.b64encode(b"image bytes").decode("ascii")
    assert content[1]["image_url"]["url"] == expected_url


def test_qwen_client_uses_png_mime_type():
    http = FakeHttpClient({"choices": [{"message": {"content": "ok"}}]})
    client = QwenClient("http://model", "sk", "model", http_client=http)

    client.chat(prompt="p", image_bytes=b"\x89PNG\r\n\x1a\npayload")

    image_url = http.calls[0]["json"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")


def test_qwen_client_sends_multiple_image_urls():
    http = FakeHttpClient({"choices": [{"message": {"content": "ok"}}]})
    client = QwenClient("http://model", "sk", "model", http_client=http)

    client.chat(prompt="Compare.", image_bytes=[b"\xff\xd8\xffone", b"\x89PNG\r\n\x1a\ntwo"])

    content = http.calls[0]["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Compare."}
    assert [item["type"] for item in content[1:]] == ["image_url", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")


def test_qwen_client_rejects_empty_choices():
    http = FakeHttpClient({"choices": []})
    client = QwenClient("http://model", "sk", "model", http_client=http)

    try:
        client.chat(prompt="p", image_bytes=b"image")
    except ValueError as exc:
        assert str(exc) == "model response did not include message content"
    else:
        raise AssertionError("expected ValueError")

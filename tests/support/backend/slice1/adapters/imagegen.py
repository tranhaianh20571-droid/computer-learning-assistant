"""Separately configured OpenAI-compatible image output (not vision input)."""
import base64
import io
import json
from contextlib import contextmanager
import httpx
from PIL import Image, ImageOps
from .openai import OpenAIAdapter
from .base import AdapterError, validate_endpoint
from ...net_policy import addresses_allowed, is_literal_address


class ImageGenerationAdapter(OpenAIAdapter):
    kind = "image_generation"
    protocol = "openai_image"
    capabilities = ("image_generate",)

    def __init__(self, *, resolver=None, **options):
        import socket
        super().__init__(**options)
        self.resolver = resolver or socket.getaddrinfo

    def _destination(self, operation="generations"):
        from ..model_catalog import canonical_endpoint
        endpoint = canonical_endpoint(self.endpoint)
        import socket
        from urllib.parse import urlsplit
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise AdapterError("invalid_endpoint", status=400)
        base = httpx.URL(endpoint)
        try:
            infos = self.resolver(base.host, base.port or 443, 0, socket.SOCK_STREAM)
        except OSError:
            raise AdapterError("connection_error", status=502) from None
        try:
            addresses = [item[4][0] for item in infos]
            if not addresses_allowed(addresses, from_hostname=not is_literal_address(base.host)):
                raise ValueError("nonpublic")
        except (ValueError, IndexError, TypeError):
            raise AdapterError("invalid_endpoint", status=400) from None
        url = httpx.URL(f"{endpoint}/images/{operation}").copy_with(host=addresses[0])
        return url, base.netloc.decode("ascii"), base.host.encode("ascii")

    def _client_or_new(self):
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False)
        return self._client

    @contextmanager
    def _http_client(self):
        yield self._client_or_new()

    def _map_status(self, status, payload):
        if status >= 500:
            raise AdapterError("provider_unavailable", status=502)
        if 300 <= status < 400:
            raise AdapterError("invalid_response", status=502)
        return super()._map_status(status, payload)

    def _image_request(self, payload, reference=None):
        from time import monotonic
        from .request_metrics import observe
        validate_endpoint(self.endpoint)
        if getattr(self, "authorize_request", None):
            self.authorize_request()
        started = monotonic()
        failure = None
        try:
            url, host_header, sni = self._destination("edits" if reference else "generations")
            if getattr(self, "authorize_request", None):
                self.authorize_request()
            headers = {**self.headers, "Accept": "application/json", "Host": host_header}
            extensions = {"sni_hostname": sni}
            request = {"json": payload}
            if reference:
                if not isinstance(reference, bytes) or len(reference) > 8 * 1024 * 1024:
                    raise AdapterError("invalid_field", status=400)
                headers.pop("Content-Type", None)
                request = {"data": {name: str(value) for name, value in payload.items()},
                    "files": {"image": ("reference.png", reference, "image/png")}}
            with self._http_client() as client:
                with client.stream("POST", str(url), **request, headers=headers, timeout=self.timeout,
                    follow_redirects=False, extensions=extensions) as response:
                    self._map_status(response.status_code, {})
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if getattr(self, "authorize_request", None):
                            self.authorize_request()
                        if len(raw) + len(chunk) > self.max_response_bytes:
                            raise AdapterError("response_too_large", status=502)
                        raw.extend(chunk)
            return json.loads(raw)
        except httpx.TimeoutException:
            failure = AdapterError("timeout", status=504)
            raise failure from None
        except httpx.HTTPError:
            failure = AdapterError("connection_error", status=502)
            raise failure from None
        except (ValueError, TypeError):
            failure = AdapterError("invalid_response", status=502)
            raise failure from None
        except Exception as exc:
            failure = exc
            raise
        finally:
            observe(self, started, exc=failure)

    def generate_image(self, description: str) -> bytes:
        if not description or len(description) > 2000:
            raise AdapterError("invalid_field", status=400)
        body = self._image_request({"model": self.model_name, "prompt": description, "n": 1,
            "size": "1024x1024", "response_format": "b64_json"})
        data = body.get("data") if isinstance(body, dict) else None
        encoded = data[0].get("b64_json") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(encoded, str) or len(encoded) > 12_000_000:
            raise AdapterError("invalid_response", status=502)
        try:
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 16_000_000:
                    raise ValueError("dimensions")
                image = ImageOps.fit(image.convert("RGB"), (960, 720))
                output = io.BytesIO(); image.save(output, format="PNG")
                return output.getvalue()
        except Exception:
            raise AdapterError("invalid_response", status=502) from None

    def _probe(self, capability):
        if capability != "image_generate":
            raise AdapterError("unsupported_capability", status=422)
        self.generate_image("A simple black ink line on white paper, no text. Synthetic capability test.")

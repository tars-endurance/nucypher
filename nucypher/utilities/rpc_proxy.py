import os
from typing import Any, Dict, List, Optional

from nucypher.config.constants import NUCYPHER_ENVVAR_ERPC_ENABLED
from nucypher.utilities.logging import Logger

_TRUE_VALUES = {"1", "true", "yes"}

logger = Logger("eRPC")


def is_erpc_enabled() -> bool:
    """Check whether the eRPC proxy feature is enabled via environment."""
    return os.environ.get(NUCYPHER_ENVVAR_ERPC_ENABLED, "").lower() in _TRUE_VALUES


# Cache TTL policy: DKG-critical calls (eth_call) must never be cached.
# Finalized/immutable data can be cached aggressively.
_TACO_CACHE_TTLS = {
    "eth_call": 0,
    "eth_sendRawTransaction": 0,
    "eth_getLogs": 2,
    "eth_blockNumber": 4,
    "eth_gasPrice": 12,
    "eth_getBalance": 4,
    "eth_getTransactionCount": 4,
    "eth_getBlockByNumber": 300,
    "eth_getBlockByHash": 3600,
    "eth_getTransactionReceipt": 3600,
    "eth_chainId": 86400,
}


def build_erpc_config(
    endpoints: Dict[int, List[str]],
    project_id: str = "taco-ursula",
    server_port: int = 4000,
    metrics_port: int = 4001,
    log_level: str = "info",
    cache_max_items: int = 10_000,
):
    """Build an ERPCConfig from Ursula's chain endpoints.

    Parameters
    ----------
    endpoints :
        Mapping of chain_id → list of RPC URLs, exactly as stored in
        ``UrsulaConfiguration.condition_blockchain_endpoints`` (plus
        ``eth_endpoint`` / ``polygon_endpoint``).
    project_id :
        eRPC project identifier (appears in proxy URLs).
    server_port :
        Local port for the eRPC HTTP proxy.
    metrics_port :
        Local port for the eRPC metrics endpoint.
    log_level :
        eRPC log verbosity (trace/debug/info/warn/error).
    cache_max_items :
        Maximum in-memory cache entries.

    Returns
    -------
    erpc.ERPCConfig
        Fully configured eRPC config ready to start a process.

    Raises
    ------
    ImportError
        If ``erpc-py`` is not installed.
    """
    from erpc import CacheConfig, ERPCConfig

    cache = CacheConfig(
        max_items=cache_max_items,
        method_ttls=dict(_TACO_CACHE_TTLS),
    )

    config = ERPCConfig(
        project_id=project_id,
        upstreams=dict(endpoints),
        server_host="127.0.0.1",
        server_port=server_port,
        metrics_host="127.0.0.1",
        metrics_port=metrics_port,
        log_level=log_level,
        cache=cache,
    )

    return config


_CHAINLIST_REPO = "nucypher/chainlist"
_CHAINLIST_BRANCH = "main"
_CHAINLIST_URL = (
    "https://raw.githubusercontent.com/{repo}/{branch}/{domain}.json"
)
_CHAINLIST_TIMEOUT = 10  # seconds


def _fetch_chainlist(domain_name: str) -> Dict[int, List[str]]:
    """Fetch public RPC endpoints from nucypher/chainlist for a given domain.

    Downloads ``{domain}.json`` from the chainlist repository at runtime.
    Returns a mapping of chain_id → [url, ...].  On any failure (network,
    parse, timeout), logs a warning and returns an empty dict — never
    blocks or crashes startup.
    """
    import urllib.request

    url = _CHAINLIST_URL.format(
        repo=_CHAINLIST_REPO,
        branch=_CHAINLIST_BRANCH,
        domain=domain_name,
    )
    logger.info(f"Fetching public RPC endpoints from {url}")

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "nucypher-ursula"},
        )
        with urllib.request.urlopen(req, timeout=_CHAINLIST_TIMEOUT) as resp:
            import json as _json

            raw = _json.loads(resp.read().decode())
    except Exception as e:
        logger.warn(f"Failed to fetch chainlist for {domain_name}: {e}")
        return {}

    # Keys are string chain IDs, values are lists of URLs
    endpoints: Dict[int, List[str]] = {}
    for chain_id_str, urls in raw.items():
        try:
            chain_id = int(chain_id_str)
        except (ValueError, TypeError):
            continue
        if isinstance(urls, list):
            endpoints[chain_id] = [u for u in urls if isinstance(u, str)]

    logger.info(
        f"Loaded {len(endpoints)} chains from chainlist "
        f"({sum(len(v) for v in endpoints.values())} total endpoints)"
    )
    return endpoints


_MAX_PUBLIC_ENDPOINTS_PER_CHAIN = 5


def _enrich_with_chainlist(
    endpoints: Dict[int, List[str]],
    domain_name: str,
    max_per_chain: int = _MAX_PUBLIC_ENDPOINTS_PER_CHAIN,
) -> Dict[int, List[str]]:
    """Merge public chainlist RPCs into operator-configured endpoints.

    Operator endpoints are kept at the front of each list (highest
    priority for eRPC).  Chainlist endpoints are appended as fallbacks,
    but only for chains the operator is already using — we don't add
    chains the operator didn't configure.

    At most ``max_per_chain`` public endpoints are added per chain to
    keep the eRPC startup time reasonable (the Go binary probes each
    upstream during initialization).
    """
    chainlist = _fetch_chainlist(domain_name)
    if not chainlist:
        return endpoints

    enriched = {k: list(v) for k, v in endpoints.items()}
    added = 0

    for chain_id, operator_urls in enriched.items():
        public_urls = chainlist.get(chain_id, [])
        chain_added = 0
        for url in public_urls:
            if chain_added >= max_per_chain:
                break
            if url not in operator_urls:
                operator_urls.append(url)
                added += 1
                chain_added += 1

    if added:
        logger.info(
            f"Enriched operator endpoints with {added} public RPCs "
            f"from chainlist (max {max_per_chain} per chain)"
        )
    return enriched


def collect_endpoints(
    eth_endpoint: Optional[str],
    polygon_endpoint: Optional[str],
    condition_blockchain_endpoints: Optional[Dict[int, List[str]]],
    domain,
    enrich: bool = True,
) -> Dict[int, List[str]]:
    """Collect all chain endpoints from Ursula's configuration into a unified map.

    Mirrors the logic in ``UrsulaConfiguration.configure_condition_blockchain_endpoints``
    without modifying any config state.

    When ``enrich`` is True (the default), appends free public RPC endpoints
    from ``nucypher/chainlist`` as fallback upstreams for every chain the
    operator has configured.

    Parameters
    ----------
    eth_endpoint :
        Primary Ethereum RPC URL.
    polygon_endpoint :
        Primary Polygon RPC URL.
    condition_blockchain_endpoints :
        Additional per-chain endpoints from config.
    domain :
        The TACo domain (provides ``eth_chain.id``, ``polygon_chain.id``,
        and ``name`` for chainlist lookup).
    enrich :
        If True, merge public endpoints from nucypher/chainlist.

    Returns
    -------
    dict
        chain_id → [url, ...] mapping.
    """
    endpoints: Dict[int, List[str]] = {}

    if condition_blockchain_endpoints:
        for chain_id, urls in condition_blockchain_endpoints.items():
            chain_id = int(chain_id)
            if isinstance(urls, str):
                urls = [urls]
            endpoints[chain_id] = list(urls)

    if eth_endpoint:
        eth_chain_id = domain.eth_chain.id
        chain_urls = endpoints.setdefault(eth_chain_id, [])
        if eth_endpoint not in chain_urls:
            chain_urls.append(eth_endpoint)

    if polygon_endpoint:
        polygon_chain_id = domain.polygon_chain.id
        chain_urls = endpoints.setdefault(polygon_chain_id, [])
        if polygon_endpoint not in chain_urls:
            chain_urls.append(polygon_endpoint)

    # Enrich with public RPCs from nucypher/chainlist
    if enrich:
        domain_name = getattr(domain, "name", str(domain)).lower()
        endpoints = _enrich_with_chainlist(endpoints, domain_name)

    return endpoints


def rewrite_endpoints(
    config,
    eth_endpoint: Optional[str],
    polygon_endpoint: Optional[str],
    condition_blockchain_endpoints: Dict[int, List[str]],
    domain,
) -> tuple[Optional[str], Optional[str], Dict[int, List[str]]]:
    """Rewrite provider URLs to route through the local eRPC proxy.

    Returns new (eth_endpoint, polygon_endpoint, condition_blockchain_endpoints)
    with URLs pointed at ``http://127.0.0.1:<port>/<project>/evm/<chain_id>``.
    The original values are NOT modified.
    """
    eth_chain_id = domain.eth_chain.id
    polygon_chain_id = domain.polygon_chain.id

    new_eth = config.endpoint_url(eth_chain_id) if eth_endpoint else eth_endpoint
    new_polygon = (
        config.endpoint_url(polygon_chain_id) if polygon_endpoint else polygon_endpoint
    )

    new_condition_endpoints = {}
    for chain_id, urls in condition_blockchain_endpoints.items():
        if urls:
            new_condition_endpoints[chain_id] = [config.endpoint_url(int(chain_id))]

    return new_eth, new_polygon, new_condition_endpoints


class RPCProxyHealthCheck:
    """Twisted LoopingCall health monitor for the eRPC proxy.

    Periodically checks process liveness and scrapes eRPC Prometheus
    metrics to surface request counts, error rates, and cache stats
    directly in Ursula's log stream.
    """

    INTERVAL = 60  # seconds

    def __init__(self, rpc_proxy: "RPCProxy"):
        from twisted.internet import reactor
        from twisted.internet.task import LoopingCall

        self._proxy = rpc_proxy
        self.log = Logger("eRPC")
        self._task = LoopingCall(self.run)
        self._task.clock = reactor
        self._last_request_count = 0
        self._check_count = 0

    @property
    def running(self) -> bool:
        return self._task.running

    def start(self) -> None:
        if not self.running:
            d = self._task.start(interval=self.INTERVAL, now=False)
            d.addErrback(self._handle_error)

    def stop(self) -> None:
        if self.running:
            self._task.stop()

    MAX_RESTART_ATTEMPTS = 3

    def run(self) -> None:
        self._check_count += 1
        if not (self._proxy._process and self._proxy._process.is_running):
            if self._proxy._active:
                self.log.warn("eRPC proxy process died — attempting restart")
                self._attempt_restart()
            return

        # Scrape lightweight stats from eRPC Prometheus metrics
        stats = self._scrape_stats()
        if stats:
            total_reqs = stats.get("total_requests", 0)
            delta = total_reqs - self._last_request_count
            self._last_request_count = total_reqs

            # Log a periodic summary (every 5 checks = ~5 min)
            if self._check_count % 5 == 0 or delta > 0:
                cache_hits = stats.get("cache_hits", 0)
                cache_misses = stats.get("cache_misses", 0)
                errors = stats.get("errors", 0)
                pid = self._proxy._process.pid
                self.log.info(
                    f"eRPC proxy: {delta} reqs (total: {total_reqs}), "
                    f"cache {cache_hits}/{cache_misses} hit/miss, "
                    f"{errors} errors, PID {pid}"
                )
        else:
            self.log.debug(f"eRPC proxy alive (PID {self._proxy._process.pid})")

    def _scrape_stats(self) -> Optional[Dict[str, int]]:
        """Scrape key counters from eRPC's Prometheus metrics endpoint."""
        import urllib.request

        metrics_url = (
            f"http://127.0.0.1:{self._proxy._erpc_config.metrics_port}/metrics"
        )
        try:
            with urllib.request.urlopen(metrics_url, timeout=2) as resp:
                body = resp.read().decode()
        except Exception:
            return None

        stats: Dict[str, int] = {}
        for line in body.splitlines():
            if line.startswith("#"):
                continue
            # Total requests proxied
            if "erpc_requests_received_total" in line and "{" not in line:
                stats["total_requests"] = int(float(line.split()[-1]))
            # Aggregate cache hits/misses
            elif "erpc_cache_hits_total" in line and "{" not in line:
                stats["cache_hits"] = int(float(line.split()[-1]))
            elif "erpc_cache_misses_total" in line and "{" not in line:
                stats["cache_misses"] = int(float(line.split()[-1]))
            # Errors
            elif "erpc_errors_total" in line and "{" not in line:
                stats["errors"] = int(float(line.split()[-1]))
        return stats if stats else None

    def _attempt_restart(self) -> None:
        """Try to restart the eRPC process when it dies unexpectedly."""
        if not hasattr(self, '_restart_count'):
            self._restart_count = 0

        self._restart_count += 1
        if self._restart_count > self.MAX_RESTART_ATTEMPTS:
            self.log.error(
                f"eRPC proxy has died {self._restart_count} times — "
                f"giving up. Ursula is running without RPC proxy. "
                f"Manual restart required."
            )
            return

        try:
            from erpc import ERPCProcess
            self.log.info(
                f"Restarting eRPC proxy (attempt {self._restart_count}/"
                f"{self.MAX_RESTART_ATTEMPTS})..."
            )
            # Re-use existing config
            self._proxy._process = ERPCProcess(config=self._proxy._erpc_config)
            self._proxy._process.start()
            self.log.info(
                f"eRPC proxy restarted (PID {self._proxy._process.pid})"
            )
            self._restart_count = 0  # Reset on success
        except Exception as e:
            self.log.warn(f"eRPC restart failed: {e}")

    def _handle_error(self, failure) -> None:
        self.log.warn(f"eRPC health check error:\n{failure.getTraceback().rstrip()}")


class RPCProxy:
    """Manages the eRPC proxy process alongside Ursula.

    Designed to be instantiated during Ursula startup and stopped during
    shutdown.  If the proxy fails to start, falls back silently to direct
    RPC endpoints (no crash, no disruption).
    """

    def __init__(
        self,
        erpc_config,
        original_eth_endpoint: str,
        original_polygon_endpoint: str,
        original_condition_endpoints: Dict[int, List[str]],
        domain,
    ):
        self.log = Logger(self.__class__.__name__)
        self._erpc_config = erpc_config
        self._domain = domain

        # Originals — preserved for fallback
        self._original_eth_endpoint = original_eth_endpoint
        self._original_polygon_endpoint = original_polygon_endpoint
        self._original_condition_endpoints = dict(original_condition_endpoints)

        # Active endpoints — start as originals, rewritten on successful start
        self.eth_endpoint = original_eth_endpoint
        self.polygon_endpoint = original_polygon_endpoint
        self.condition_blockchain_endpoints = dict(original_condition_endpoints)

        self._process = None
        self._active = False
        self._health_check = None

    @classmethod
    def from_config(
        cls,
        eth_endpoint: str,
        polygon_endpoint: str,
        condition_blockchain_endpoints: Dict[int, List[str]],
        domain,
    ) -> "RPCProxy":
        """Create an RPCProxy from raw endpoint values (no character dependency)."""
        endpoints = collect_endpoints(
            eth_endpoint=eth_endpoint,
            polygon_endpoint=polygon_endpoint,
            condition_blockchain_endpoints=condition_blockchain_endpoints,
            domain=domain,
        )

        try:
            erpc_config = build_erpc_config(endpoints=endpoints)
        except ImportError:
            logger.warn("erpc-py is not installed — cannot build eRPC config")
            raise

        return cls(
            erpc_config=erpc_config,
            original_eth_endpoint=eth_endpoint,
            original_polygon_endpoint=polygon_endpoint,
            original_condition_endpoints=condition_blockchain_endpoints or {},
            domain=domain,
        )

    @classmethod
    def from_ursula_config(cls, config) -> "RPCProxy":
        """Create an RPCProxy from an UrsulaConfiguration instance."""
        endpoints = collect_endpoints(
            eth_endpoint=config.eth_endpoint,
            polygon_endpoint=config.polygon_endpoint,
            condition_blockchain_endpoints=config.condition_blockchain_endpoints,
            domain=config.domain,
        )

        try:
            erpc_config = build_erpc_config(endpoints=endpoints)
        except ImportError:
            logger.warn("erpc-py is not installed — cannot build eRPC config")
            raise

        return cls(
            erpc_config=erpc_config,
            original_eth_endpoint=config.eth_endpoint,
            original_polygon_endpoint=config.polygon_endpoint,
            original_condition_endpoints=config.condition_blockchain_endpoints,
            domain=config.domain,
        )

    @classmethod
    def from_ursula(cls, ursula) -> "RPCProxy":
        """Create an RPCProxy from a live Ursula instance."""
        endpoints = collect_endpoints(
            eth_endpoint=ursula.eth_endpoint,
            polygon_endpoint=ursula.polygon_endpoint,
            condition_blockchain_endpoints=ursula.condition_blockchain_endpoints,
            domain=ursula.domain,
        )

        try:
            erpc_config = build_erpc_config(endpoints=endpoints)
        except ImportError:
            logger.warn("erpc-py is not installed — cannot build eRPC config")
            raise

        return cls(
            erpc_config=erpc_config,
            original_eth_endpoint=ursula.eth_endpoint,
            original_polygon_endpoint=ursula.polygon_endpoint,
            original_condition_endpoints=ursula.condition_blockchain_endpoints or {},
            domain=ursula.domain,
        )

    @property
    def is_active(self) -> bool:
        """Whether the eRPC proxy is running and endpoints are rewritten."""
        return self._active

    def start(self, health_timeout: int = 300) -> bool:
        """Start the eRPC proxy process and wait for it to be ready.

        Blocks until eRPC is responding to HTTP requests (even 502),
        then rewrites Ursula's endpoints to route through the proxy.
        This must complete before ``create_character()`` calls
        ``connect()`` — otherwise Ursula's initial ``get_block``
        calls will fail.

        Parameters
        ----------
        health_timeout :
            Maximum seconds to wait for eRPC to start responding.
            Default 300 (5 minutes).

        Returns ``True`` if the proxy started and endpoints were
        rewritten.  ``False`` on failure (falls back to direct
        endpoints).
        """
        import time

        try:
            from erpc import ERPCProcess
        except ImportError:
            self.log.warn("erpc-py is not installed — running without RPC proxy")
            return False

        try:
            self._process = ERPCProcess(config=self._erpc_config)
            self._process.start()
        except Exception:
            import traceback as _tb

            stderr = ""
            if self._process and hasattr(self._process, '_process'):
                inner = self._process._process
                if inner and inner.stderr:
                    try:
                        stderr = inner.stderr.read().decode(errors="replace")
                    except Exception:
                        pass
            msg = "eRPC proxy process failed to start — using direct endpoints:\n"
            msg += _tb.format_exc().rstrip()
            if stderr:
                msg += f"\neRPC stderr: {stderr[:500]}"
            self.log.warn(msg)
            self._fallback()
            return False

        self.log.info(f"eRPC proxy process started (PID {self._process.pid})")

        # Wait for eRPC to start responding to HTTP requests.
        # The Go binary returns 502 while probing upstreams, then 200
        # once at least one upstream is healthy.  We accept any HTTP
        # response as "ready" — eRPC handles upstream failover internally.
        health_url = self._erpc_config.health_url
        self.log.info(f"Waiting for eRPC to respond (up to {health_timeout}s)...")

        deadline = time.monotonic() + health_timeout
        check_count = 0
        last_log = 0

        while time.monotonic() < deadline:
            if not self._process.is_running:
                self.log.warn(
                    "eRPC process died during startup — using direct endpoints"
                )
                self._fallback()
                return False

            if self._is_erpc_responding():
                self.log.info(
                    f"eRPC is responding after {check_count} checks — "
                    f"rewriting endpoints to proxy"
                )
                break

            check_count += 1
            elapsed = int(time.monotonic() - (deadline - health_timeout))

            if elapsed - last_log >= 15:
                last_log = elapsed
                self.log.info(
                    f"eRPC health wait: {elapsed}s elapsed, {check_count} checks, "
                    f"health_url={health_url}"
                )

            time.sleep(1)
        else:
            self.log.warn(
                f"eRPC did not respond within {health_timeout}s — "
                f"using direct endpoints"
            )
            self._fallback()
            return False

        # Rewrite endpoints to route through the proxy
        self._activate_endpoints()
        self.log.info(
            f"eRPC proxy active (PID {self._process.pid}), endpoints rewritten"
        )
        return True

    def _is_erpc_responding(self) -> bool:
        """Check if eRPC can actually proxy an RPC request.

        Sends a real ``eth_chainId`` JSON-RPC call through the proxy
        for the first configured chain.  Returns True only if we get
        a valid JSON-RPC response — not just an HTTP response.

        This is stricter than checking the health endpoint because
        eRPC returns 502 immediately on startup before any upstream
        is probed.  We need at least one working upstream.
        """
        import json as _json
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError

        # Pick the first chain to test
        chains = sorted(self._erpc_config.upstreams.keys())
        if not chains:
            return False

        test_chain = chains[0]
        project_id = self._erpc_config.project_id
        port = self._erpc_config.server_port
        url = f"http://127.0.0.1:{port}/{project_id}/evm/{test_chain}"

        payload = _json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_chainId",
            "params": [],
            "id": 1,
        }).encode()

        try:
            req = Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=10) as resp:
                body = _json.loads(resp.read().decode())
                # Valid JSON-RPC response has "result"
                return "result" in body
        except (HTTPError, URLError, OSError, ValueError, KeyError):
            return False

    def _activate_endpoints(self) -> None:
        """Rewrite endpoints to route through the eRPC proxy."""
        (
            self.eth_endpoint,
            self.polygon_endpoint,
            self.condition_blockchain_endpoints,
        ) = rewrite_endpoints(
            config=self._erpc_config,
            eth_endpoint=self._original_eth_endpoint,
            polygon_endpoint=self._original_polygon_endpoint,
            condition_blockchain_endpoints=self._original_condition_endpoints,
            domain=self._domain,
        )
        self._active = True

        # Start periodic health monitoring
        try:
            self._health_check = RPCProxyHealthCheck(self)
            self._health_check.start()
        except Exception:
            pass  # Health check is best-effort

    def stop(self) -> None:
        """Stop the eRPC proxy and restore original endpoints."""
        if self._health_check and self._health_check.running:
            self._health_check.stop()
        if self._process and self._process.is_running:
            try:
                self._process.stop()
                self.log.info("eRPC proxy stopped")
            except Exception:
                self.log.warn(
                    f"Error stopping eRPC proxy:\n{__import__('traceback').format_exc().rstrip()}"
                )
        self._fallback()

    def _fallback(self) -> None:
        """Restore original endpoints (direct RPC, no proxy)."""
        self.eth_endpoint = self._original_eth_endpoint
        self.polygon_endpoint = self._original_polygon_endpoint
        self.condition_blockchain_endpoints = dict(self._original_condition_endpoints)
        self._active = False
        self._process = None

    @property
    def health_url(self) -> Optional[str]:
        """eRPC health check URL, or None if not running."""
        if self._active:
            return self._erpc_config.health_url
        return None

    def status_info(self) -> Dict[str, Any]:
        """Return eRPC proxy status for inclusion in Ursula's status JSON."""
        info: Dict[str, Any] = {
            "active": self._active,
        }

        # PID and chains are available as soon as the process is started,
        # even before the background health thread has activated the proxy.
        if self._process:
            info["pid"] = self._process.pid
        if self._erpc_config and self._erpc_config.upstreams:
            info["chains"] = sorted(self._erpc_config.upstreams.keys())
            info["upstream_count"] = sum(
                len(urls) for urls in self._erpc_config.upstreams.values()
            )
            # Full upstream map: chain_id → [url, ...]
            info["upstreams"] = {
                str(k): list(v)
                for k, v in sorted(self._erpc_config.upstreams.items())
            }

        if not self._active:
            info["status"] = "warming up" if self._process else "inactive"
            return info

        info["status"] = "active"
        info["server_port"] = self._erpc_config.server_port
        info["metrics_port"] = self._erpc_config.metrics_port
        info["health_url"] = self.health_url

        # Proxied endpoints
        info["eth_endpoint"] = self.eth_endpoint
        info["polygon_endpoint"] = self.polygon_endpoint
        info["condition_blockchain_endpoints"] = {
            str(k): v for k, v in self.condition_blockchain_endpoints.items()
        }

        # Cache config
        cache = self._erpc_config.cache
        if cache and cache.method_ttls:
            info["cache_policies"] = {
                method: f"{ttl}s" for method, ttl in cache.method_ttls.items()
            }

        return info

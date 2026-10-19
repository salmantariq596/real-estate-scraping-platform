"""
test_webshare_proxies.py — Standalone connectivity check for your Webshare
proxy list, BEFORE wiring it into the full scraper.

What it does:
  - Reads PINCALI_PROXY_LIST from your .env (or set PROXY_TEST_LIST env var
    directly if you'd rather not touch .env yet).
  - Hits https://api.ipify.org through EACH proxy.
  - Prints per-proxy: OK/FAIL, latency, and the exit IP it got back.
  - At the end, confirms whether you're actually seeing 10 DIFFERENT exit
    IPs (real rotation) vs the same IP repeating (misconfigured proxy).

Run it from the project root:
    pip install httpx python-dotenv
    python test_webshare_proxies.py

This never sends your credentials anywhere except directly to Webshare's
own proxy servers (the same way the real scraper will) and to
api.ipify.org (a public IP-echo service, no auth, no logging concerns).
"""
import asyncio
import os
import sys
import time

try:
    import httpx
except ImportError:
    print("Run: pip install httpx python-dotenv")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if .env vars are already exported in the shell

TEST_URL = "https://api.ipify.org?format=json"
TIMEOUT_SECONDS = 12


def get_proxy_list() -> list[str]:
    raw = os.getenv("PROXY_TEST_LIST") or os.getenv("PINCALI_PROXY_LIST", "")
    proxies = [p.strip() for p in raw.split(",") if p.strip()]
    if not proxies:
        print("No proxies found. Set PINCALI_PROXY_LIST in your .env, or")
        print("export PROXY_TEST_LIST='http://user:pass@host1:port1,http://user:pass@host2:port2,...'")
        sys.exit(1)
    return proxies


async def check_proxy(proxy_url: str, index: int) -> dict:
    label = f"proxy-{index}"
    # Mask credentials in any printed output
    display = proxy_url
    if "@" in proxy_url:
        scheme_and_creds, host_part = proxy_url.rsplit("@", 1)
        display = f"{scheme_and_creds.split('://')[0]}://***:***@{host_part}"

    start = time.time()
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(TEST_URL)
            latency_ms = round((time.time() - start) * 1000)
            if resp.status_code == 200:
                exit_ip = resp.json().get("ip", "unknown")
                return {"label": label, "display": display, "ok": True,
                        "exit_ip": exit_ip, "latency_ms": latency_ms}
            return {"label": label, "display": display, "ok": False,
                     "error": f"HTTP {resp.status_code}", "latency_ms": latency_ms}
    except Exception as e:
        latency_ms = round((time.time() - start) * 1000)
        return {"label": label, "display": display, "ok": False,
                 "error": str(e), "latency_ms": latency_ms}


async def main():
    proxies = get_proxy_list()
    print(f"Testing {len(proxies)} Webshare proxies against {TEST_URL}\n")

    results = await asyncio.gather(
        *[check_proxy(p, i + 1) for i, p in enumerate(proxies)]
    )

    ok_count = 0
    exit_ips = set()
    for r in results:
        if r["ok"]:
            ok_count += 1
            exit_ips.add(r["exit_ip"])
            print(f"  ✅ {r['label']:10s} {r['display']:40s} exit_ip={r['exit_ip']:16s} {r['latency_ms']}ms")
        else:
            print(f"  ❌ {r['label']:10s} {r['display']:40s} FAILED: {r['error']} ({r['latency_ms']}ms)")

    print(f"\n{ok_count}/{len(proxies)} proxies working.")
    if ok_count > 0:
        print(f"{len(exit_ips)} distinct exit IP(s) seen across working proxies.")
        if len(exit_ips) < ok_count:
            print("⚠️  Some proxies returned the SAME exit IP — check with Webshare")
            print("   whether these are meant to be distinct IPs or a shared gateway.")
        else:
            print("✅ Every working proxy returned a distinct exit IP — rotation looks correct.")

    if ok_count < len(proxies):
        print("\nFor failed proxies, double-check in the Webshare dashboard:")
        print("  - Proxy List page: is the IP still 'valid'? (Webshare replaces dead IPs)")
        print("  - Authentication Method: Username/Password selected (not IP Authorization)")
        print("  - If using IP Authorization instead, your test machine's IP needs to be")
        print("    whitelisted in the Webshare dashboard first.")


if __name__ == "__main__":
    asyncio.run(main())

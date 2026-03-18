"""Prometheus exporter for Docker container metrics on Docker Desktop/Windows."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import docker
from docker.errors import DockerException
from prometheus_client import Gauge, start_http_server

PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "bayreuthukraine")
PORT = int(os.getenv("DOCKER_METRICS_EXPORTER_PORT", "9200"))
SCRAPE_INTERVAL = float(os.getenv("DOCKER_METRICS_EXPORTER_INTERVAL", "10"))

CONTAINER_CPU_PERCENT = Gauge(
    "bayreuth_container_cpu_percent",
    "Docker container CPU usage percent.",
    ["project", "service", "container_name"],
)
CONTAINER_MEMORY_USAGE = Gauge(
    "bayreuth_container_memory_usage_bytes",
    "Docker container memory usage in bytes.",
    ["project", "service", "container_name"],
)
CONTAINER_MEMORY_LIMIT = Gauge(
    "bayreuth_container_memory_limit_bytes",
    "Docker container memory limit in bytes.",
    ["project", "service", "container_name"],
)
CONTAINER_NETWORK_RX = Gauge(
    "bayreuth_container_network_rx_bytes_total",
    "Docker container received bytes.",
    ["project", "service", "container_name"],
)
CONTAINER_NETWORK_TX = Gauge(
    "bayreuth_container_network_tx_bytes_total",
    "Docker container transmitted bytes.",
    ["project", "service", "container_name"],
)
CONTAINER_RUNNING = Gauge(
    "bayreuth_container_running",
    "Whether the container is currently running.",
    ["project", "service", "container_name"],
)
CONTAINER_RESTART_COUNT = Gauge(
    "bayreuth_container_restart_count",
    "Docker container restart count.",
    ["project", "service", "container_name"],
)
CONTAINER_HEALTH = Gauge(
    "bayreuth_container_health_status",
    "Container health status as numeric value (healthy=1, starting=0.5, unhealthy=0, none=-1).",
    ["project", "service", "container_name"],
)
ENGINE_CONTAINERS_RUNNING = Gauge(
    "bayreuth_docker_engine_containers_running",
    "Number of running containers reported by Docker engine.",
)
ENGINE_CONTAINERS_TOTAL = Gauge(
    "bayreuth_docker_engine_containers_total",
    "Total number of containers reported by Docker engine.",
)
ENGINE_IMAGES_TOTAL = Gauge(
    "bayreuth_docker_engine_images_total",
    "Total number of images reported by Docker engine.",
)


def _cpu_percent(stats: dict[str, Any]) -> float:
    """Calculate Docker-compatible CPU percent from a stats payload."""
    cpu_stats = stats.get("cpu_stats", {})
    precpu_stats = stats.get("precpu_stats", {})
    cpu_total = cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
    precpu_total = precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
    system_total = cpu_stats.get("system_cpu_usage", 0)
    presystem_total = precpu_stats.get("system_cpu_usage", 0)
    cpu_delta = cpu_total - precpu_total
    system_delta = system_total - presystem_total
    online_cpus = cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or []) or 1
    if cpu_delta <= 0 or system_delta <= 0:
        return 0.0
    return float(cpu_delta) / float(system_delta) * float(online_cpus) * 100.0


def _network_totals(stats: dict[str, Any]) -> tuple[float, float]:
    """Aggregate received and transmitted bytes across all container interfaces."""
    rx = 0.0
    tx = 0.0
    for payload in (stats.get("networks") or {}).values():
        rx += float(payload.get("rx_bytes", 0))
        tx += float(payload.get("tx_bytes", 0))
    return rx, tx


def _health_value(container: docker.models.containers.Container) -> float:
    """Map Docker health status strings to numeric values for Prometheus."""
    state = (container.attrs.get("State") or {})
    health = (state.get("Health") or {}).get("Status")
    if health == "healthy":
        return 1.0
    if health == "starting":
        return 0.5
    if health == "unhealthy":
        return 0.0
    return -1.0


def _update_engine_metrics(client: docker.DockerClient) -> None:
    """Update Docker engine-wide gauges."""
    info = client.info()
    ENGINE_CONTAINERS_RUNNING.set(float(info.get("ContainersRunning", 0)))
    ENGINE_CONTAINERS_TOTAL.set(float(info.get("Containers", 0)))
    ENGINE_IMAGES_TOTAL.set(float(info.get("Images", 0)))


def _update_container_metrics(client: docker.DockerClient) -> None:
    """Refresh per-container Prometheus gauges for compose services."""
    containers = client.containers.list(all=True, filters={"label": f"com.docker.compose.project={PROJECT}"})
    seen_labels: set[tuple[str, str, str]] = set()

    for container in containers:
        labels = container.labels or {}
        service = labels.get("com.docker.compose.service", "unknown")
        container_name = container.name
        label_values = (PROJECT, service, container_name)
        seen_labels.add(label_values)

        stats = container.stats(stream=False)
        memory_stats = stats.get("memory_stats", {})
        memory_usage = float(memory_stats.get("usage", 0))
        memory_limit = float(memory_stats.get("limit", 0))
        rx, tx = _network_totals(stats)

        CONTAINER_CPU_PERCENT.labels(*label_values).set(_cpu_percent(stats))
        CONTAINER_MEMORY_USAGE.labels(*label_values).set(memory_usage)
        CONTAINER_MEMORY_LIMIT.labels(*label_values).set(memory_limit)
        CONTAINER_NETWORK_RX.labels(*label_values).set(rx)
        CONTAINER_NETWORK_TX.labels(*label_values).set(tx)
        CONTAINER_RUNNING.labels(*label_values).set(1.0 if container.status == "running" else 0.0)
        CONTAINER_RESTART_COUNT.labels(*label_values).set(float(container.attrs.get("RestartCount", 0)))
        CONTAINER_HEALTH.labels(*label_values).set(_health_value(container))


def main() -> None:
    """Run the exporter HTTP server and refresh gauges in a polling loop."""
    logging.basicConfig(level=logging.INFO)
    start_http_server(PORT)
    client = docker.DockerClient(base_url=os.getenv("DOCKER_HOST", "unix://var/run/docker.sock"))
    logging.info("docker-metrics-exporter started on :%s for project=%s", PORT, PROJECT)

    while True:
        try:
            _update_engine_metrics(client)
            _update_container_metrics(client)
        except DockerException:
            logging.exception("Failed to refresh Docker metrics")
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()

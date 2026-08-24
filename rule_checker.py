from __future__ import annotations

import argparse
import ipaddress
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class Finding:
    rule_id: str
    severity: str
    message: str
    evidence: str
    recommendation: str


def value(case: dict[str, Any], key: str) -> str:
    item = case.get(key, "")
    if item is None or pd.isna(item):
        return ""
    return str(item).strip()


def split_values(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(";") if item.strip()]


def check_addressing(case: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    ip_text = value(case, "pc_ip")
    mask_text = value(case, "pc_mask")
    gateway_text = value(case, "pc_gateway")

    if not ip_text:
        return findings

    if ip_text.startswith("169.254."):
        findings.append(
            Finding(
                "APIPA_ADDRESS",
                "High",
                "The client has a link-local address, indicating DHCP failure.",
                f"Client address is {ip_text}.",
                "Inspect DHCP service, pool availability, VLAN path, and relay configuration.",
            )
        )

    if not mask_text:
        return findings

    try:
        client = ipaddress.ip_interface(f"{ip_text}/{mask_text}")
    except ValueError as exc:
        findings.append(
            Finding(
                "INVALID_IP_OR_MASK",
                "High",
                "The client IP address or subnet mask is invalid.",
                str(exc),
                "Correct the client IP address and subnet mask.",
            )
        )
        return findings

    if gateway_text:
        try:
            gateway = ipaddress.ip_address(gateway_text)
            if gateway not in client.network:
                findings.append(
                    Finding(
                        "GATEWAY_MISMATCH",
                        "High",
                        "The default gateway is outside the client's local subnet.",
                        f"{gateway} is not in {client.network}.",
                        "Configure a gateway address belonging to the client's local subnet.",
                    )
                )
        except ValueError:
            findings.append(
                Finding(
                    "INVALID_GATEWAY",
                    "High",
                    "The default gateway address is invalid.",
                    f"Configured gateway: {gateway_text}.",
                    "Configure a valid IPv4 gateway address.",
                )
            )

    for peer_text in split_values(value(case, "peer_ips")):
        try:
            peer = ipaddress.ip_address(peer_text)
        except ValueError:
            continue

        if peer == client.ip:
            findings.append(
                Finding(
                    "DUPLICATE_IP",
                    "Critical",
                    "The client's IP address duplicates another known device address.",
                    f"Duplicate address: {client.ip}.",
                    "Assign a unique address and clear stale ARP entries after correction.",
                )
            )

    return findings


def check_interface(case: dict[str, Any]) -> list[Finding]:
    status = value(case, "interface_status").lower()
    if not status:
        return []

    if "administratively down" in status:
        return [
            Finding(
                "ADMIN_DOWN",
                "High",
                "The interface is administratively disabled.",
                f"Interface status: {status}.",
                "Verify the intended port, then issue no shutdown.",
            )
        ]

    if "err-disabled" in status or "secure-shutdown" in status:
        return [
            Finding(
                "ERR_DISABLED",
                "High",
                "The interface is disabled because of an error or security violation.",
                f"Interface status: {status}.",
                "Find and correct the violation before recovering the interface.",
            )
        ]

    if status in {"down/down", "down", "notconnect"}:
        return [
            Finding(
                "LINK_DOWN",
                "Critical",
                "The physical or data-link interface is down.",
                f"Interface status: {status}.",
                "Check cabling, peer status, interface configuration, and power.",
            )
        ]

    return []


def check_vlan(case: dict[str, Any]) -> list[Finding]:
    required_vlan = value(case, "required_vlan")
    known_vlans = set(split_values(value(case, "known_vlans")))

    if required_vlan and known_vlans and required_vlan not in known_vlans:
        return [
            Finding(
                "MISSING_VLAN",
                "High",
                f"Required VLAN {required_vlan} does not exist in the known VLAN list.",
                f"Known VLANs: {', '.join(sorted(known_vlans))}.",
                f"Create VLAN {required_vlan} and verify access and trunk assignments.",
            )
        ]

    return []


def route_matches(destination: ipaddress.IPv4Network, route_text: str) -> bool:
    try:
        route = ipaddress.ip_network(route_text, strict=False)
    except ValueError:
        return False

    return destination.subnet_of(route) or destination == route


def check_routes(case: dict[str, Any]) -> list[Finding]:
    destination_text = value(case, "destination_network")
    route_entries = split_values(value(case, "route_table"))

    if not destination_text or not route_entries:
        return []

    try:
        destination = ipaddress.ip_network(destination_text, strict=False)
    except ValueError:
        return [
            Finding(
                "INVALID_DESTINATION_NETWORK",
                "Medium",
                "The expected destination network is invalid.",
                f"Destination: {destination_text}.",
                "Correct the destination network used by the case.",
            )
        ]

    if not any(route_matches(destination, route) for route in route_entries):
        return [
            Finding(
                "MISSING_ROUTE",
                "High",
                f"No route covers destination {destination}.",
                f"Known routes: {', '.join(route_entries)}.",
                "Add or dynamically learn a route, then verify the return path.",
            )
        ]

    return []


def check_case(case: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(check_addressing(case))
    findings.extend(check_interface(case))
    findings.extend(check_vlan(case))
    findings.extend(check_routes(case))
    return findings


def load_case(csv_path: Path, case_id: str) -> dict[str, Any]:
    dataframe = pd.read_csv(csv_path, dtype=str).fillna("")
    selected = dataframe[dataframe["case_id"] == case_id]

    if selected.empty:
        raise ValueError(f"Case {case_id} was not found in {csv_path}")

    return selected.iloc[0].to_dict()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic NetSage checks.")
    parser.add_argument("--cases", default="data/cases.csv")
    parser.add_argument("--case-id")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    dataframe = pd.read_csv(args.cases, dtype=str).fillna("")

    if args.case_id:
        dataframe = dataframe[dataframe["case_id"] == args.case_id]
    elif not args.all:
        parser.error("Use --case-id NS-001 or --all")

    if dataframe.empty:
        raise SystemExit("No matching cases found.")

    for _, row in dataframe.iterrows():
        findings = [asdict(item) for item in check_case(row.to_dict())]
        print(
            json.dumps(
                {
                    "case_id": row["case_id"],
                    "title": row["title"],
                    "findings": findings,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Cloud provider options data for connector configuration.

This module contains all the configuration data for cloud providers including:
- AWS regions, instance types, and Ubuntu AMIs
- GCP zones, machine types, and Ubuntu images
- Azure locations, VM sizes, and Ubuntu images

All data is structured as rich objects with metadata for frontend display.
"""

from pydantic import BaseModel

# --- Rich Option Models ---

class RegionOption(BaseModel):
    """A region/zone/location option with display metadata."""
    code: str
    name: str


class InstanceTypeOption(BaseModel):
    """An instance/machine type option with specs."""
    code: str
    vcpus: float
    memory_gb: float
    architecture: str  # "x86_64" or "arm64"
    description: str


# --- Ubuntu 24.04 LTS Image Configuration ---
# Easily configurable constants for OS images

UBUNTU_VERSION = "24.04"
UBUNTU_CODENAME = "Noble Numbat"

# GCP Ubuntu images
GCP_UBUNTU_IMAGE_X86 = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
GCP_UBUNTU_IMAGE_ARM = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-arm64"

# Azure Ubuntu images (publisher:offer:sku:version format)
AZURE_UBUNTU_IMAGE_X86 = {
    "publisher": "Canonical",
    "offer": "ubuntu-24_04-lts",
    "sku": "server",
    "version": "latest",
}
AZURE_UBUNTU_IMAGE_ARM = {
    "publisher": "Canonical",
    "offer": "ubuntu-24_04-lts",
    "sku": "server-arm64",
    "version": "latest",
}


# --- AWS Ubuntu 24.04 LTS AMIs by Region and Architecture ---
# Format: (region, architecture) -> ami_id
# Architecture: "amd64" for x86_64, "arm64" for ARM/Graviton

AWS_UBUNTU_AMIS: dict[tuple[str, str], str] = {
    # US Regions
    ("us-east-1", "amd64"): "ami-0136735c2bb5cf5bf",
    ("us-east-1", "arm64"): "ami-00cdb36f35bd8af7d",
    ("us-east-2", "amd64"): "ami-025b3ba8491a26d32",
    ("us-east-2", "arm64"): "ami-0970fa3799b2fd400",
    ("us-west-1", "amd64"): "ami-02a00652440d3572d",
    ("us-west-1", "arm64"): "ami-03e75ebb2a538195b",
    ("us-west-2", "amd64"): "ami-068914dc1588b6848",
    ("us-west-2", "arm64"): "ami-022b02c9589f293cd",
    # US Gov Regions
    ("us-gov-east-1", "amd64"): "ami-07dda913f87fbae59",
    ("us-gov-east-1", "arm64"): "ami-082a3a032b2e4323c",
    ("us-gov-west-1", "amd64"): "ami-08caa896ea395d5e5",
    ("us-gov-west-1", "arm64"): "ami-014c734fe26ab1d4d",
    # EU Regions
    ("eu-west-1", "amd64"): "ami-092b91d47c6c8baa5",
    ("eu-west-1", "arm64"): "ami-056b141cce065e0c1",
    ("eu-west-2", "amd64"): "ami-0f19b6377d50fa855",
    ("eu-west-2", "arm64"): "ami-00aa8c535f886e354",
    ("eu-west-3", "amd64"): "ami-0b19907aada3a1fc5",
    ("eu-west-3", "arm64"): "ami-00e0664a89c0fb14c",
    ("eu-central-1", "amd64"): "ami-0aad10862ade98f27",
    ("eu-central-1", "arm64"): "ami-0a96a698343e1007e",
    ("eu-central-2", "amd64"): "ami-098b18824b4a69a1d",
    ("eu-central-2", "arm64"): "ami-0d487f4a22a593217",
    ("eu-north-1", "amd64"): "ami-02e70da87e10e9324",
    ("eu-north-1", "arm64"): "ami-04ec7341c50b3f6f5",
    ("eu-south-1", "amd64"): "ami-074741e812858087b",
    ("eu-south-1", "arm64"): "ami-0333024279d1eb9e5",
    ("eu-south-2", "amd64"): "ami-08961d58f5f9b2a33",
    ("eu-south-2", "arm64"): "ami-09a58f258c3659d4c",
    # AP Regions
    ("ap-east-1", "amd64"): "ami-0db71cf3198b283bc",
    ("ap-east-1", "arm64"): "ami-0e6cb937466df078b",
    ("ap-east-2", "amd64"): "ami-07a8c0b29b476d235",
    ("ap-east-2", "arm64"): "ami-08fe667de82fa97c0",
    ("ap-south-1", "amd64"): "ami-09b041abcb4daa286",
    ("ap-south-1", "arm64"): "ami-0524921c40ecaf6a8",
    ("ap-south-2", "amd64"): "ami-0ff17bad4e09a4d55",
    ("ap-south-2", "arm64"): "ami-0508f5c420b8cb7b9",
    ("ap-southeast-1", "amd64"): "ami-0a6a4c524bc36f8f1",
    ("ap-southeast-1", "arm64"): "ami-05f18f4d71a1c5d3a",
    ("ap-southeast-2", "amd64"): "ami-057dca46468e8cd4d",
    ("ap-southeast-2", "arm64"): "ami-0e4fbc08e3a1720ca",
    ("ap-southeast-3", "amd64"): "ami-00ce94f13c4bae76d",
    ("ap-southeast-3", "arm64"): "ami-01a0755a603eb00ad",
    ("ap-southeast-4", "amd64"): "ami-09e2b0cfff014c888",
    ("ap-southeast-4", "arm64"): "ami-0b11cd51a40806dca",
    ("ap-southeast-5", "amd64"): "ami-0a90cee41f6cabb93",
    ("ap-southeast-5", "arm64"): "ami-0a4fce5cbf3703f5e",
    ("ap-southeast-6", "amd64"): "ami-0c8c644b7d4848366",
    ("ap-southeast-6", "arm64"): "ami-0f176290f710da56d",
    ("ap-southeast-7", "amd64"): "ami-08a38e8efb0485a3a",
    ("ap-southeast-7", "arm64"): "ami-0aa87d66e3628e78f",
    ("ap-northeast-1", "amd64"): "ami-025ece6a3a0e7558f",
    ("ap-northeast-1", "arm64"): "ami-0b9234df7a90ffdef",
    ("ap-northeast-2", "amd64"): "ami-096dce0fcb85a808f",
    ("ap-northeast-2", "arm64"): "ami-083465ac652b895e4",
    ("ap-northeast-3", "amd64"): "ami-0aa79fdb9c594383b",
    ("ap-northeast-3", "arm64"): "ami-0f4652a1963ddf68d",
    # CA Regions
    ("ca-central-1", "amd64"): "ami-0f85dfb29f6f783a1",
    ("ca-central-1", "arm64"): "ami-0050b92dbff47eff9",
    ("ca-west-1", "amd64"): "ami-0daf3ff945ab150d4",
    ("ca-west-1", "arm64"): "ami-0db4c7a807654b54f",
    # SA Region
    ("sa-east-1", "amd64"): "ami-01dcb95628698598b",
    ("sa-east-1", "arm64"): "ami-06ba5c68822b97a14",
    # AF Region
    ("af-south-1", "amd64"): "ami-08fedaff0a92c0708",
    ("af-south-1", "arm64"): "ami-0878f36822b7953f9",
    # ME Regions
    ("me-south-1", "amd64"): "ami-0220b9c69b017e4ea",
    ("me-south-1", "arm64"): "ami-042a4ad346c2bf3f2",
    ("me-central-1", "amd64"): "ami-04c9d06e580f46505",
    ("me-central-1", "arm64"): "ami-083ef9403aedc48f9",
    # IL Region
    ("il-central-1", "amd64"): "ami-0cd18fa6e651e4151",
    ("il-central-1", "arm64"): "ami-057a86fed61cb67d8",
    # MX Region
    ("mx-central-1", "amd64"): "ami-0a57f819c3fccde4f",
    ("mx-central-1", "arm64"): "ami-0e55ff56de8967570",
    # CN Regions
    ("cn-north-1", "amd64"): "ami-0ad706323460f0872",
    ("cn-north-1", "arm64"): "ami-09c5a0b109545cb67",
    ("cn-northwest-1", "amd64"): "ami-0d0403fe1e1c154bf",
    ("cn-northwest-1", "arm64"): "ami-06cd44249c9768ac6",
}


# --- AWS Regions with Friendly Names ---

AWS_REGIONS: list[RegionOption] = [
    # US Regions
    RegionOption(code="us-east-1", name="N. Virginia, USA (us-east-1)"),
    RegionOption(code="us-east-2", name="Ohio, USA (us-east-2)"),
    RegionOption(code="us-west-1", name="N. California, USA (us-west-1)"),
    RegionOption(code="us-west-2", name="Oregon, USA (us-west-2)"),
    # US Gov Regions
    RegionOption(code="us-gov-east-1", name="US-East (GovCloud)"),
    RegionOption(code="us-gov-west-1", name="US-West (GovCloud)"),
    # EU Regions
    RegionOption(code="eu-west-1", name="Ireland (eu-west-1)"),
    RegionOption(code="eu-west-2", name="London, UK (eu-west-2)"),
    RegionOption(code="eu-west-3", name="Paris, France (eu-west-3)"),
    RegionOption(code="eu-central-1", name="Frankfurt, Germany (eu-central-1)"),
    RegionOption(code="eu-central-2", name="Zurich, Switzerland (eu-central-2)"),
    RegionOption(code="eu-north-1", name="Stockholm, Sweden (eu-north-1)"),
    RegionOption(code="eu-south-1", name="Milan, Italy (eu-south-1)"),
    RegionOption(code="eu-south-2", name="Spain (eu-south-2)"),
    # AP Regions
    RegionOption(code="ap-east-1", name="Hong Kong (ap-east-1)"),
    RegionOption(code="ap-east-2", name="Hong Kong 2 (ap-east-2)"),
    RegionOption(code="ap-south-1", name="Mumbai, India (ap-south-1)"),
    RegionOption(code="ap-south-2", name="Hyderabad, India (ap-south-2)"),
    RegionOption(code="ap-southeast-1", name="Singapore (ap-southeast-1)"),
    RegionOption(code="ap-southeast-2", name="Sydney, Australia (ap-southeast-2)"),
    RegionOption(code="ap-southeast-3", name="Jakarta, Indonesia (ap-southeast-3)"),
    RegionOption(code="ap-southeast-4", name="Melbourne, Australia (ap-southeast-4)"),
    RegionOption(code="ap-southeast-5", name="Malaysia (ap-southeast-5)"),
    RegionOption(code="ap-southeast-6", name="Thailand (ap-southeast-6)"),
    RegionOption(code="ap-southeast-7", name="Thailand 2 (ap-southeast-7)"),
    RegionOption(code="ap-northeast-1", name="Tokyo, Japan (ap-northeast-1)"),
    RegionOption(code="ap-northeast-2", name="Seoul, South Korea (ap-northeast-2)"),
    RegionOption(code="ap-northeast-3", name="Osaka, Japan (ap-northeast-3)"),
    # CA Regions
    RegionOption(code="ca-central-1", name="Montreal, Canada (ca-central-1)"),
    RegionOption(code="ca-west-1", name="Calgary, Canada (ca-west-1)"),
    # SA Region
    RegionOption(code="sa-east-1", name="São Paulo, Brazil (sa-east-1)"),
    # AF Region
    RegionOption(code="af-south-1", name="Cape Town, South Africa (af-south-1)"),
    # ME Regions
    RegionOption(code="me-south-1", name="Bahrain (me-south-1)"),
    RegionOption(code="me-central-1", name="UAE (me-central-1)"),
    # IL Region
    RegionOption(code="il-central-1", name="Tel Aviv, Israel (il-central-1)"),
    # MX Region
    RegionOption(code="mx-central-1", name="Querétaro, Mexico (mx-central-1)"),
    # CN Regions
    RegionOption(code="cn-north-1", name="Beijing, China (cn-north-1)"),
    RegionOption(code="cn-northwest-1", name="Ningxia, China (cn-northwest-1)"),
]


# --- AWS Instance Types (t3 + t4g families) ---

AWS_INSTANCE_TYPES: list[InstanceTypeOption] = [
    # t3 family (Intel Xeon, x86_64)
    InstanceTypeOption(code="t3.micro", vcpus=2, memory_gb=1.0, architecture="x86_64",
                       description="2 vCPUs, 1 GB RAM, Intel Xeon"),
    InstanceTypeOption(code="t3.small", vcpus=2, memory_gb=2.0, architecture="x86_64",
                       description="2 vCPUs, 2 GB RAM, Intel Xeon"),
    InstanceTypeOption(code="t3.medium", vcpus=2, memory_gb=4.0, architecture="x86_64",
                       description="2 vCPUs, 4 GB RAM, Intel Xeon"),
    InstanceTypeOption(code="t3.large", vcpus=2, memory_gb=8.0, architecture="x86_64",
                       description="2 vCPUs, 8 GB RAM, Intel Xeon"),
    InstanceTypeOption(code="t3.xlarge", vcpus=4, memory_gb=16.0, architecture="x86_64",
                       description="4 vCPUs, 16 GB RAM, Intel Xeon"),
    InstanceTypeOption(code="t3.2xlarge", vcpus=8, memory_gb=32.0, architecture="x86_64",
                       description="8 vCPUs, 32 GB RAM, Intel Xeon"),
    # t4g family (Graviton2, arm64)
    InstanceTypeOption(code="t4g.micro", vcpus=2, memory_gb=1.0, architecture="arm64",
                       description="2 vCPUs, 1 GB RAM, Graviton2"),
    InstanceTypeOption(code="t4g.small", vcpus=2, memory_gb=2.0, architecture="arm64",
                       description="2 vCPUs, 2 GB RAM, Graviton2"),
    InstanceTypeOption(code="t4g.medium", vcpus=2, memory_gb=4.0, architecture="arm64",
                       description="2 vCPUs, 4 GB RAM, Graviton2"),
    InstanceTypeOption(code="t4g.large", vcpus=2, memory_gb=8.0, architecture="arm64",
                       description="2 vCPUs, 8 GB RAM, Graviton2"),
    InstanceTypeOption(code="t4g.xlarge", vcpus=4, memory_gb=16.0, architecture="arm64",
                       description="4 vCPUs, 16 GB RAM, Graviton2"),
    InstanceTypeOption(code="t4g.2xlarge", vcpus=8, memory_gb=32.0, architecture="arm64",
                       description="8 vCPUs, 32 GB RAM, Graviton2"),
]


# --- GCP Zones with Friendly Names (using -a suffix for each region) ---

GCP_ZONES: list[RegionOption] = [
    # US
    RegionOption(code="us-central1-a", name="Iowa, USA (us-central1-a)"),
    RegionOption(code="us-east1-a", name="South Carolina, USA (us-east1-a)"),
    RegionOption(code="us-east4-a", name="N. Virginia, USA (us-east4-a)"),
    RegionOption(code="us-east5-a", name="Columbus, USA (us-east5-a)"),
    RegionOption(code="us-south1-a", name="Dallas, USA (us-south1-a)"),
    RegionOption(code="us-west1-a", name="Oregon, USA (us-west1-a)"),
    RegionOption(code="us-west2-a", name="Los Angeles, USA (us-west2-a)"),
    RegionOption(code="us-west3-a", name="Salt Lake City, USA (us-west3-a)"),
    RegionOption(code="us-west4-a", name="Las Vegas, USA (us-west4-a)"),
    # North America
    RegionOption(code="northamerica-northeast1-a", name="Montreal, Canada (northamerica-northeast1-a)"),
    RegionOption(code="northamerica-northeast2-a", name="Toronto, Canada (northamerica-northeast2-a)"),
    # South America
    RegionOption(code="southamerica-east1-a", name="São Paulo, Brazil (southamerica-east1-a)"),
    RegionOption(code="southamerica-west1-a", name="Santiago, Chile (southamerica-west1-a)"),
    # Europe
    RegionOption(code="europe-central2-a", name="Warsaw, Poland (europe-central2-a)"),
    RegionOption(code="europe-north1-a", name="Hamina, Finland (europe-north1-a)"),
    RegionOption(code="europe-southwest1-a", name="Madrid, Spain (europe-southwest1-a)"),
    RegionOption(code="europe-west1-a", name="St. Ghislain, Belgium (europe-west1-a)"),
    RegionOption(code="europe-west2-a", name="London, UK (europe-west2-a)"),
    RegionOption(code="europe-west3-a", name="Frankfurt, Germany (europe-west3-a)"),
    RegionOption(code="europe-west4-a", name="Eemshaven, Netherlands (europe-west4-a)"),
    RegionOption(code="europe-west6-a", name="Zurich, Switzerland (europe-west6-a)"),
    RegionOption(code="europe-west8-a", name="Milan, Italy (europe-west8-a)"),
    RegionOption(code="europe-west9-a", name="Paris, France (europe-west9-a)"),
    RegionOption(code="europe-west10-a", name="Berlin, Germany (europe-west10-a)"),
    RegionOption(code="europe-west12-a", name="Turin, Italy (europe-west12-a)"),
    # Asia
    RegionOption(code="asia-east1-a", name="Changhua County, Taiwan (asia-east1-a)"),
    RegionOption(code="asia-east2-a", name="Hong Kong (asia-east2-a)"),
    RegionOption(code="asia-northeast1-a", name="Tokyo, Japan (asia-northeast1-a)"),
    RegionOption(code="asia-northeast2-a", name="Osaka, Japan (asia-northeast2-a)"),
    RegionOption(code="asia-northeast3-a", name="Seoul, South Korea (asia-northeast3-a)"),
    RegionOption(code="asia-south1-a", name="Mumbai, India (asia-south1-a)"),
    RegionOption(code="asia-south2-a", name="Delhi, India (asia-south2-a)"),
    RegionOption(code="asia-southeast1-a", name="Singapore (asia-southeast1-a)"),
    RegionOption(code="asia-southeast2-a", name="Jakarta, Indonesia (asia-southeast2-a)"),
    # Australia
    RegionOption(code="australia-southeast1-a", name="Sydney, Australia (australia-southeast1-a)"),
    RegionOption(code="australia-southeast2-a", name="Melbourne, Australia (australia-southeast2-a)"),
    # Middle East
    RegionOption(code="me-central1-a", name="Doha, Qatar (me-central1-a)"),
    RegionOption(code="me-central2-a", name="Dammam, Saudi Arabia (me-central2-a)"),
    RegionOption(code="me-west1-a", name="Tel Aviv, Israel (me-west1-a)"),
    # Africa
    RegionOption(code="africa-south1-a", name="Johannesburg, South Africa (africa-south1-a)"),
]


# --- GCP Machine Types (e2 + t2a families) ---

GCP_MACHINE_TYPES: list[InstanceTypeOption] = [
    # e2 family (x86_64, shared-core and standard)
    InstanceTypeOption(code="e2-micro", vcpus=0.25, memory_gb=1.0, architecture="x86_64",
                       description="Shared-core, 1 GB RAM"),
    InstanceTypeOption(code="e2-small", vcpus=0.5, memory_gb=2.0, architecture="x86_64",
                       description="Shared-core, 2 GB RAM"),
    InstanceTypeOption(code="e2-medium", vcpus=1, memory_gb=4.0, architecture="x86_64",
                       description="Shared-core, 4 GB RAM"),
    InstanceTypeOption(code="e2-standard-2", vcpus=2, memory_gb=8.0, architecture="x86_64",
                       description="2 vCPUs, 8 GB RAM"),
    InstanceTypeOption(code="e2-standard-4", vcpus=4, memory_gb=16.0, architecture="x86_64",
                       description="4 vCPUs, 16 GB RAM"),
    # t2a family (Ampere Altra, arm64)
    InstanceTypeOption(code="t2a-standard-1", vcpus=1, memory_gb=4.0, architecture="arm64",
                       description="1 vCPU, 4 GB RAM, Ampere Altra"),
    InstanceTypeOption(code="t2a-standard-2", vcpus=2, memory_gb=8.0, architecture="arm64",
                       description="2 vCPUs, 8 GB RAM, Ampere Altra"),
    InstanceTypeOption(code="t2a-standard-4", vcpus=4, memory_gb=16.0, architecture="arm64",
                       description="4 vCPUs, 16 GB RAM, Ampere Altra"),
]


# --- Azure Locations with Friendly Names ---

AZURE_LOCATIONS: list[RegionOption] = [
    # US
    RegionOption(code="eastus", name="Virginia, USA (East US)"),
    RegionOption(code="eastus2", name="Virginia, USA (East US 2)"),
    RegionOption(code="centralus", name="Iowa, USA (Central US)"),
    RegionOption(code="northcentralus", name="Illinois, USA (North Central US)"),
    RegionOption(code="southcentralus", name="Texas, USA (South Central US)"),
    RegionOption(code="westcentralus", name="Wyoming, USA (West Central US)"),
    RegionOption(code="westus", name="California, USA (West US)"),
    RegionOption(code="westus2", name="Washington, USA (West US 2)"),
    RegionOption(code="westus3", name="Arizona, USA (West US 3)"),
    # Canada
    RegionOption(code="canadacentral", name="Toronto, Canada (Canada Central)"),
    RegionOption(code="canadaeast", name="Quebec City, Canada (Canada East)"),
    # Brazil
    RegionOption(code="brazilsouth", name="São Paulo, Brazil (Brazil South)"),
    # Europe
    RegionOption(code="northeurope", name="Dublin, Ireland (North Europe)"),
    RegionOption(code="westeurope", name="Amsterdam, Netherlands (West Europe)"),
    RegionOption(code="uksouth", name="London, UK (UK South)"),
    RegionOption(code="ukwest", name="Cardiff, UK (UK West)"),
    RegionOption(code="francecentral", name="Paris, France (France Central)"),
    RegionOption(code="germanywestcentral", name="Frankfurt, Germany (Germany West Central)"),
    RegionOption(code="swedencentral", name="Gävle, Sweden (Sweden Central)"),
    RegionOption(code="norwayeast", name="Oslo, Norway (Norway East)"),
    RegionOption(code="switzerlandnorth", name="Zurich, Switzerland (Switzerland North)"),
    RegionOption(code="italynorth", name="Milan, Italy (Italy North)"),
    RegionOption(code="polandcentral", name="Warsaw, Poland (Poland Central)"),
    # Asia Pacific
    RegionOption(code="eastasia", name="Hong Kong (East Asia)"),
    RegionOption(code="southeastasia", name="Singapore (Southeast Asia)"),
    RegionOption(code="japaneast", name="Tokyo, Japan (Japan East)"),
    RegionOption(code="japanwest", name="Osaka, Japan (Japan West)"),
    RegionOption(code="koreacentral", name="Seoul, South Korea (Korea Central)"),
    RegionOption(code="koreasouth", name="Busan, South Korea (Korea South)"),
    RegionOption(code="centralindia", name="Pune, India (Central India)"),
    RegionOption(code="southindia", name="Chennai, India (South India)"),
    RegionOption(code="westindia", name="Mumbai, India (West India)"),
    # Australia
    RegionOption(code="australiaeast", name="Sydney, Australia (Australia East)"),
    RegionOption(code="australiasoutheast", name="Melbourne, Australia (Australia Southeast)"),
    # Middle East
    RegionOption(code="uaenorth", name="Dubai, UAE (UAE North)"),
    RegionOption(code="qatarcentral", name="Doha, Qatar (Qatar Central)"),
    RegionOption(code="israelcentral", name="Tel Aviv, Israel (Israel Central)"),
    # Africa
    RegionOption(code="southafricanorth", name="Johannesburg, South Africa (South Africa North)"),
]


# --- Azure VM Sizes (B-series v2: Bsv2 for x86_64, Bpsv2 for ARM64) ---
# B-series v2 supports Gen2 hypervisor (required for Ubuntu 24.04 LTS)
# Note: ARM64 (Bpsv2) typically requires quota request in most subscriptions

AZURE_VM_SIZES: list[InstanceTypeOption] = [
    # Bsv2 series (Intel Xeon, x86_64, Gen2 supported) - Burstable
    InstanceTypeOption(code="Standard_B2ls_v2", vcpus=2, memory_gb=4.0, architecture="x86_64",
                       description="2 vCPUs, 4 GB RAM, Burstable"),
    InstanceTypeOption(code="Standard_B2s_v2", vcpus=2, memory_gb=8.0, architecture="x86_64",
                       description="2 vCPUs, 8 GB RAM, Burstable"),
    InstanceTypeOption(code="Standard_B4ls_v2", vcpus=4, memory_gb=8.0, architecture="x86_64",
                       description="4 vCPUs, 8 GB RAM, Burstable"),
    InstanceTypeOption(code="Standard_B4s_v2", vcpus=4, memory_gb=16.0, architecture="x86_64",
                       description="4 vCPUs, 16 GB RAM, Burstable"),
    # Bpsv2 series (Ampere Altra, arm64, Gen2 supported) - Requires quota request
    InstanceTypeOption(code="Standard_B2pls_v2", vcpus=2, memory_gb=4.0, architecture="arm64",
                       description="2 vCPUs, 4 GB RAM, ARM64 (requires quota)"),
    InstanceTypeOption(code="Standard_B2ps_v2", vcpus=2, memory_gb=8.0, architecture="arm64",
                       description="2 vCPUs, 8 GB RAM, ARM64 (requires quota)"),
    InstanceTypeOption(code="Standard_B4pls_v2", vcpus=4, memory_gb=8.0, architecture="arm64",
                       description="4 vCPUs, 8 GB RAM, ARM64 (requires quota)"),
    InstanceTypeOption(code="Standard_B4ps_v2", vcpus=4, memory_gb=16.0, architecture="arm64",
                       description="4 vCPUs, 16 GB RAM, ARM64 (requires quota)"),
]


# --- Helper Functions ---

# Build lookup dictionaries for architecture by instance type code
_AWS_INSTANCE_TYPE_ARCH: dict[str, str] = {t.code: t.architecture for t in AWS_INSTANCE_TYPES}
_GCP_MACHINE_TYPE_ARCH: dict[str, str] = {t.code: t.architecture for t in GCP_MACHINE_TYPES}
_AZURE_VM_SIZE_ARCH: dict[str, str] = {t.code: t.architecture for t in AZURE_VM_SIZES}


def get_aws_architecture(instance_type: str) -> str:
    """Get the architecture for an AWS instance type.

    Looks up the architecture from AWS_INSTANCE_TYPES.
    Returns "amd64" for x86_64 instances, "arm64" for Graviton.
    Defaults to "amd64" if instance type is not found.
    """
    arch = _AWS_INSTANCE_TYPE_ARCH.get(instance_type)
    if arch == "arm64":
        return "arm64"
    return "amd64"


def get_gcp_architecture(machine_type: str) -> str:
    """Get the architecture for a GCP machine type.

    Looks up the architecture from GCP_MACHINE_TYPES.
    Returns "arm64" for Tau T2A instances, "x86_64" for others.
    Defaults to "x86_64" if machine type is not found.
    """
    return _GCP_MACHINE_TYPE_ARCH.get(machine_type, "x86_64")


def get_azure_architecture(vm_size: str) -> str:
    """Get the architecture for an Azure VM size.

    Looks up the architecture from AZURE_VM_SIZES.
    Returns "arm64" for Ampere instances, "x86_64" for others.
    Defaults to "x86_64" if VM size is not found.
    """
    return _AZURE_VM_SIZE_ARCH.get(vm_size, "x86_64")


# --- Oxylabs Country Options ---

class CountryOption(BaseModel):
    """A country option with display metadata."""
    code: str
    name: str


OXYLABS_COUNTRIES: list[CountryOption] = [
    # "All" option for no geo-targeting
    CountryOption(code="", name="All (No geo-targeting)"),
    # North America
    CountryOption(code="US", name="United States"),
    CountryOption(code="CA", name="Canada"),
    CountryOption(code="MX", name="Mexico"),
    # Europe
    CountryOption(code="GB", name="United Kingdom"),
    CountryOption(code="DE", name="Germany"),
    CountryOption(code="FR", name="France"),
    CountryOption(code="IT", name="Italy"),
    CountryOption(code="ES", name="Spain"),
    CountryOption(code="NL", name="Netherlands"),
    CountryOption(code="BE", name="Belgium"),
    CountryOption(code="AT", name="Austria"),
    CountryOption(code="CH", name="Switzerland"),
    CountryOption(code="PL", name="Poland"),
    CountryOption(code="SE", name="Sweden"),
    CountryOption(code="NO", name="Norway"),
    CountryOption(code="DK", name="Denmark"),
    CountryOption(code="FI", name="Finland"),
    CountryOption(code="IE", name="Ireland"),
    CountryOption(code="PT", name="Portugal"),
    CountryOption(code="CZ", name="Czech Republic"),
    CountryOption(code="RO", name="Romania"),
    CountryOption(code="HU", name="Hungary"),
    CountryOption(code="GR", name="Greece"),
    CountryOption(code="UA", name="Ukraine"),
    CountryOption(code="RU", name="Russia"),
    # Asia Pacific
    CountryOption(code="JP", name="Japan"),
    CountryOption(code="KR", name="South Korea"),
    CountryOption(code="CN", name="China"),
    CountryOption(code="HK", name="Hong Kong"),
    CountryOption(code="TW", name="Taiwan"),
    CountryOption(code="SG", name="Singapore"),
    CountryOption(code="AU", name="Australia"),
    CountryOption(code="NZ", name="New Zealand"),
    CountryOption(code="IN", name="India"),
    CountryOption(code="ID", name="Indonesia"),
    CountryOption(code="TH", name="Thailand"),
    CountryOption(code="VN", name="Vietnam"),
    CountryOption(code="MY", name="Malaysia"),
    CountryOption(code="PH", name="Philippines"),
    # South America
    CountryOption(code="BR", name="Brazil"),
    CountryOption(code="AR", name="Argentina"),
    CountryOption(code="CL", name="Chile"),
    CountryOption(code="CO", name="Colombia"),
    CountryOption(code="PE", name="Peru"),
    # Middle East & Africa
    CountryOption(code="IL", name="Israel"),
    CountryOption(code="AE", name="United Arab Emirates"),
    CountryOption(code="SA", name="Saudi Arabia"),
    CountryOption(code="TR", name="Turkey"),
    CountryOption(code="ZA", name="South Africa"),
    CountryOption(code="EG", name="Egypt"),
    CountryOption(code="NG", name="Nigeria"),
]

packer {
  required_plugins {
    proxmox = {
      source  = "github.com/hashicorp/proxmox"
      version = ">= 1.2.3"
    }
  }
}

variable "proxmox_url" {
  type    = string
  default = ""
}

variable "node" {
  type = string
}

variable "vm_id" {
  type = number
}

variable "vm_name" {
  type = string
}

variable "template_name" {
  type = string
}

variable "vm_storage" {
  type = string
}

variable "bridge" {
  type = string
}

variable "memory" {
  type = number
}

variable "cores" {
  type = number
}

variable "cpu_type" {
  type = string
}

variable "cloud_init_storage_pool" {
  type = string
}

variable "iso_file" {
  type        = string
  description = "Proxmox storage reference for the ISO (e.g. local:iso/ubuntu-22.04.3-live-server-amd64.iso)."
}

variable "iso_checksum" {
  type        = string
  default     = "none"
  description = "ISO checksum (sha256:<hash>) or 'none' to skip verification."
}

source "proxmox-iso" "cloud_image" {
  proxmox_url = env("PROXMOX_URL") != "" ? env("PROXMOX_URL") : var.proxmox_url
  username    = env("PROXMOX_USERNAME")
  token       = env("PROXMOX_TOKEN")

  node          = var.node
  vm_id         = var.vm_id
  vm_name       = var.vm_name
  template_name = var.template_name

  iso_file     = var.iso_file
  iso_checksum = var.iso_checksum

  qemu_agent              = true
  scsi_controller         = "virtio-scsi-pci"
  memory                  = var.memory
  cores                   = var.cores
  cpu_type                = var.cpu_type
  insecure_skip_tls_verify = true

  cloud_init              = true
  cloud_init_storage_pool = var.cloud_init_storage_pool

  disks {
    disk_size    = "20G"
    storage_pool = var.vm_storage
    type         = "virtio"
  }

  network_adapters {
    model  = "virtio"
    bridge = var.bridge
  }

  # Ubuntu autoinstall: HTTP server for cloud-config seed
  http_directory = "http"

  boot_wait = "5s"
  boot_command = [
    "<esc><wait>",
    "linux /casper/vmlinuz quiet autoinstall ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ ---<enter>",
    "initrd /casper/initrd<enter>",
    "boot<enter>"
  ]

  ssh_username = "cloud-user"
  ssh_password = env("PACKER_SSH_PASS")
  ssh_timeout  = "30m"
}

build {
  sources = ["source.proxmox-iso.cloud_image"]

  provisioner "shell" {
    script = "provisioners/selected-recipe.sh"
  }
}

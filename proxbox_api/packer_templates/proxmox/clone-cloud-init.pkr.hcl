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

variable "clone_vm_id" {
  type = number
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

source "proxmox-clone" "cloud_image" {
  proxmox_url = env("PROXMOX_URL") != "" ? env("PROXMOX_URL") : var.proxmox_url
  username    = env("PROXMOX_USERNAME")
  token       = env("PROXMOX_TOKEN")

  node         = var.node
  clone_vm_id  = var.clone_vm_id
  vm_id        = var.vm_id
  vm_name      = var.vm_name
  template_name = var.template_name

  full_clone              = true
  cloud_init              = true
  cloud_init_storage_pool = var.cloud_init_storage_pool
  qemu_agent              = true
  scsi_controller         = "virtio-scsi-pci"
  memory                  = var.memory
  cores                   = var.cores
  cpu_type                = var.cpu_type
  insecure_skip_tls_verify = true

  network_adapters {
    model  = "virtio"
    bridge = var.bridge
  }
}

build {
  sources = ["source.proxmox-clone.cloud_image"]

  provisioner "shell" {
    script = "provisioners/selected-recipe.sh"
  }
}

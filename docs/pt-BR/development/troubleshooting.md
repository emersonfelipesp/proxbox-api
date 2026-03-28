# Troubleshooting

## Falta endpoint NetBox

Crie o endpoint com `POST /netbox/endpoint` e valide com `GET /netbox/endpoint`.

## Erros de autenticacao Proxmox

- Forneca `password` ou o par `token_name` + `token_value`.

## Problemas de CORS

- Verifique dominio/porta de origem e configuracao dos endpoints.

## Falhas de conexao com Proxmox

- Validar host, porta, credenciais e `verify_ssl`.

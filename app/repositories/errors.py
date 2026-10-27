"""Errores de la capa de persistencia. Nunca contienen payloads, consultas ni credenciales."""


class StorageError(Exception):
    """La operación de almacenamiento no pudo completarse. Quien llama debe reintentar o fallar cerrado."""

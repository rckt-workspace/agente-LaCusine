-- Catálogo comercial: precio actual y precio anterior, disponibilidad, nota de entrega,
-- fecha de extracción de la fuente y texto normalizado para búsqueda (Fase 3).
-- Los precios son referencias de la fuente; no confirman inventario ni promociones vigentes.

alter table public.products
  add column name text check (char_length(name) between 1 and 200),
  add column previous_price_cop numeric(14, 2) check (previous_price_cop is null or previous_price_cop >= 0),
  add column savings_cop numeric(14, 2) check (savings_cop is null or savings_cop >= 0),
  add column is_available boolean,
  add column delivery_note text check (delivery_note is null or char_length(delivery_note) <= 200),
  add column extracted_on date,
  add column search_text text not null default '' check (char_length(search_text) <= 6000);

-- Si hay precio anterior, debe ser mayor que el actual (de lo contrario no es un descuento real).
alter table public.products
  add constraint products_previous_price_is_higher
    check (previous_price_cop is null or price_cop is null or previous_price_cop > price_cop);

-- Filas previas a esta migración (importación CSV de Fase 2) no tienen estos campos; se completan al reimportar.
-- La importación XLSX exige name y extracted_on para cualquier fila nueva.

create index products_price_idx on public.products (price_cop);
create index products_available_idx on public.products (is_available);

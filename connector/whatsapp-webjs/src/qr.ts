// Presentación del código QR de vinculación. Solo se escribe en la consola (stdout, UTF-8).
// Nunca se pasa al registro ni se guarda en archivos. La consola de Windows debe estar en UTF-8 (chcp 65001),
// que el lanzador configura antes de arrancar este proceso.
import qrcode from "qrcode-terminal";

export function renderQr(qr: string): Promise<string> {
  return new Promise((resolve) => {
    qrcode.generate(qr, { small: true }, (art: string) => resolve(art));
  });
}

export async function printQr(qr: string, write: (text: string) => void): Promise<void> {
  const art = await renderQr(qr);
  write("\nEscanee este QR desde WhatsApp > Dispositivos vinculados. No se guarda en ningún registro.\n" + art + "\n");
}

import './globals.css';

export const metadata = {
  title: 'Sadece Sohbet — Türkçe sesli sohbet',
  description: 'Türkçe konuş, doğal bir sohbet başlat. Hesap gerekmez.',
};

export default function RootLayout({ children }) {
  return <html lang="tr"><body>{children}</body></html>;
}

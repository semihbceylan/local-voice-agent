import { DM_Sans, Manrope } from 'next/font/google';
import './globals.css';

// next/font downloads the fonts at build time and serves them from this
// site, so visitors make no request to Google.
const body = DM_Sans({ subsets: ['latin', 'latin-ext'], variable: '--font-body', display: 'swap' });
const display = Manrope({ subsets: ['latin', 'latin-ext'], variable: '--font-display', display: 'swap' });

export const metadata = {
  title: 'Sadece Sohbet — Türkçe sesli sohbet',
  description: 'Türkçe konuş, doğal bir sohbet başlat. Hesap gerekmez.',
};

export const viewport = {
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#f7f8f2' },
    { media: '(prefers-color-scheme: dark)', color: '#101a17' },
  ],
};

export default function RootLayout({ children }) {
  return <html lang="tr" className={`${body.variable} ${display.variable}`}><body>{children}</body></html>;
}

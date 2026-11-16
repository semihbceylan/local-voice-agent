import './globals.css';

export const metadata = {
  title: 'DemoShop — Voice concierge',
  description: 'A hands-free browser voice demo. No account needed.',
};

export default function RootLayout({ children }) {
  return <html lang="en"><body>{children}</body></html>;
}

/** @jsxImportSource react */

export const metadata = {
  title: "Tempo AI Slackbot",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

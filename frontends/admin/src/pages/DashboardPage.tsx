import { AppHeader } from '@/core/components/AppHeader';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/core/components/ui/card';
import { ArrowRight, MessageSquare, Settings } from 'lucide-react';
import { Link } from 'react-router-dom';

type Tool = {
  slug: string;
  to: string;
  title: string;
  description: string;
  icon: typeof MessageSquare;
};

const tools: Tool[] = [
  {
    slug: 'dm',
    to: '/circle-dm',
    title: 'Circle DM',
    description: 'Inbox, drafty AI, bulk i compose pod kontem admina Circle.',
    icon: MessageSquare,
  },
  {
    slug: 'settings',
    to: '/ustawienia',
    title: 'Ustawienia',
    description: 'Workery członkostw, AI, billing, newsletter, analityka i status połączeń API.',
    icon: Settings,
  },
];

export function DashboardPage() {
  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      <AppHeader />
      <main className="flex-1 mx-auto w-full max-w-[1400px] px-4 sm:px-6 py-10">
        <div className="mb-8">
          <h1 className="auth-title text-3xl sm:text-4xl mb-2">Panel administratora</h1>
          <p className="text-foreground/60">
            Wewnętrzne narzędzia Be Free Club. Wybierz tool z listy poniżej.
          </p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {tools.map((tool) => (
            <Link key={tool.slug} to={tool.to} className="group block">
              <Card
                glow
                className="h-full transition-all group-hover:border-primary/50 group-hover:bg-card-hover"
              >
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <div className="h-11 w-11 rounded-lg bg-primary/15 border border-primary/30 grid place-items-center">
                      <tool.icon className="h-5 w-5 text-primary" />
                    </div>
                    <ArrowRight className="h-4 w-4 text-foreground/30 group-hover:text-primary transition-colors" />
                  </div>
                  <CardTitle className="mt-3">{tool.title}</CardTitle>
                  <CardDescription>{tool.description}</CardDescription>
                </CardHeader>
                <CardContent />
              </Card>
            </Link>
          ))}
        </div>
      </main>
    </div>
  );
}

import { useEffect, useRef, type ReactNode } from "react";
import { ArrowUpRight } from "lucide-react";
import "./auth.css";
import BrandLogo from "../../ui/BrandLogo";

function Ink() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current!;
    const context = canvas.getContext("2d")!;
    function paint() {
      const { width, height } = canvas.getBoundingClientRect();
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      const small = width < 600, scale = small ? .6 : Math.min(width / 1440, 1.2);
      const strokes = small
        ? [[.87, .15, 22, 8, 1, .75], [.07, .85, 78, 17, -.35, .65], [.8, .93, 44, 8, .1, .14]]
        : [[.11, .31, 100, 19, -.4, .8], [.24, .75, 60, 7, .1, .35],
          [.83, .24, 31, 10, 1.1, .85], [.89, .65, 88, 16, -1, .14],
          [.07, .82, 160, 24, -.45, .055], [.75, .87, 120, 18, .2, .055]];
      for (const [x, y, length, thickness, angle, opacity] of strokes) {
        context.save(); context.translate(width * x, height * y); context.rotate(angle);
        context.scale(scale, scale); context.beginPath();
        context.moveTo(0, thickness * .15);
        context.bezierCurveTo(length * .13, -thickness * .65, length * .55, -thickness * .2, length, -thickness * .7);
        context.bezierCurveTo(length * .7, thickness * .05, length * .23, thickness * .65, 0, thickness * .4);
        context.quadraticCurveTo(-thickness * .16, thickness * .3, 0, thickness * .15);
        context.fillStyle = `rgba(25,26,25,${opacity})`; context.fill();
        context.restore();
      }
      const dots = small ? [[.94, .33, 1.4], [.91, .85, 2], [.2, .92, .7]]
        : [[.08, .44, 2], [.2, .25, .8], [.23, .57, 2.5], [.15, .85, 1],
          [.76, .19, 1.6], [.91, .39, .7], [.81, .56, 2.8], [.93, .81, 1.2]];
      for (const [x, y, radius] of dots) {
        context.beginPath(); context.ellipse(width * x, height * y, radius, radius * .8, .4, 0, Math.PI * 2);
        context.fillStyle = "rgba(25,26,25,.6)"; context.fill();
      }
    }
    paint();
    const observer = new ResizeObserver(paint); observer.observe(canvas);
    return () => observer.disconnect();
  }, []);
  return <canvas className="auth-ink-art" ref={ref} aria-hidden="true" />;
}

export default function AuthShell({ children, register = false }: { children: ReactNode; register?: boolean }) {
  return <div className="auth auth-ink">
    <Ink />
    <header className="auth-ink-header">
      <a className="auth-ink-brand" href="#/login" aria-label="学伴"><BrandLogo decorative /></a>
      <a className="auth-ink-link" href={register ? "#/login" : "#/register"}>
        {register ? "登录账号" : "注册账号"}<ArrowUpRight size={16} aria-hidden="true" />
      </a>
    </header>
    <main className="auth-ink-main"><div className="auth-col">
      <span className="auth-ink-eyebrow">A SPACE TO THINK</span>{children}
    </div></main>
    <footer className="auth-ink-footer"><span aria-hidden="true">学</span></footer>
  </div>;
}

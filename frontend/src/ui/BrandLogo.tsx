import "./brandLogo.css";
import logoUrl from "../assets/logo-unfold.png";

export default function BrandLogo({ decorative = false }: { decorative?: boolean }) {
  return <img className="app-logo" src={logoUrl}
    width={48} height={48} alt={decorative ? "" : "学伴"} />;
}

import { useId, useState } from "react";
import { Eye, EyeOff } from "lucide-react";
/** 表单字段原语：label + input + 可选帮助文案（认证页与管理页共用）。 */
export default function Field({ label, type, value, onChange, help, autoComplete }: any) {
  const id = useId();
  const [shown, setShown] = useState(false);
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className={type === "password" ? "auth-password" : undefined}><input
        id={id}
        className="inp"
        type={type === "password" && shown ? "text" : type}
        value={value}
        autoComplete={autoComplete}
        onChange={(e) => onChange(e.target.value)}
      />
      {type === "password" && <button type="button" aria-label={shown ? "隐藏密码" : "显示密码"}
        aria-pressed={shown} title={shown ? "隐藏密码" : "显示密码"} onClick={() => setShown(!shown)}>
        {shown ? <EyeOff size={18} /> : <Eye size={18} />}
      </button>}</div>
      {help && <span className="help">{help}</span>}
    </div>
  );
}

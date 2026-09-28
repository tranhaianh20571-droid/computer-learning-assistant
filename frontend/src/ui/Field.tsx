/** 表单字段原语：label + input + 可选帮助文案（认证页与管理页共用）。 */
export default function Field({ label, type, value, onChange, help, autoComplete }: any) {
  return (
    <div className="field">
      <label>{label}</label>
      <input
        className="inp"
        type={type}
        value={value}
        autoComplete={autoComplete}
        onChange={(e) => onChange(e.target.value)}
      />
      {help && <span className="help">{help}</span>}
    </div>
  );
}

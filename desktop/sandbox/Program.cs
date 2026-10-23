using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

// This helper is trusted. Only its child is placed in the AppContainer.
internal static class Program
{
    static string phase="profile";
    [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes { public int length; public IntPtr descriptor; public int inherit; }
    [StructLayout(LayoutKind.Sequential)] struct SecurityCapabilities { public IntPtr sid; public IntPtr capabilities; public uint count; public uint reserved; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo {
        public int cb; public IntPtr reserved, desktop, title;
        public uint x,y,xSize,ySize,xChars,yChars,fill,flags;
        public ushort show,reserved2; public IntPtr reservedPointer,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct StartupInfoEx { public StartupInfo info; public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process,thread; public uint pid,tid; }
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long processTime,jobTime; public uint flags;
        public UIntPtr minWorking,maxWorking; public uint processes;
        public UIntPtr affinity; public uint priority,scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public BasicLimits basic; public IoCounters io;
        public UIntPtr processMemory,jobMemory,peakProcess,peakJob;
    }
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int CreateAppContainerProfile(string name,string display,string description,IntPtr caps,uint count,out IntPtr sid);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int DeriveAppContainerSidFromAppContainerName(string name,out IntPtr sid);
    [DllImport("advapi32.dll", SetLastError=true)] static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool ConvertSidToStringSidW(IntPtr sid,out IntPtr text);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr pointer);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr attributes,IntPtr name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int info,ref ExtendedLimits limits,uint length);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,uint flags,ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returned);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcessW(string executable,StringBuilder args,IntPtr processAttributes,IntPtr threadAttributes,
        bool inherit,uint flags,IntPtr environment,string directory,ref StartupInfoEx startup,out ProcessInfo process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateFileW(string name,uint access,uint share,ref SecurityAttributes attributes,uint creation,uint flags,IntPtr template);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int kind);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CreatePipe(out IntPtr read,out IntPtr write,ref SecurityAttributes attributes,uint size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetHandleInformation(IntPtr handle,uint mask,uint flags);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool WriteFile(IntPtr handle,byte[] bytes,uint count,out uint written,IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,uint pid);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int kind,out int value,int size,out int returned);

    static void Require(bool value) { if (!value) throw new InvalidOperationException("win32_"+Marshal.GetLastWin32Error()); }
    static string Quote(string value) {
        var result = new StringBuilder("\""); int slashes=0;
        foreach (char ch in value) {
            if(ch=='\\') { slashes++; continue; }
            result.Append('\\', ch=='"' ? slashes*2+1 : slashes); slashes=0; result.Append(ch);
        }
        result.Append('\\',slashes*2); return result.Append('"').ToString();
    }
    static void Grant(string path,string sid,bool writable,bool recursive) {
        var info = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"icacls.exe")) {
            CreateNoWindow=true,UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true
        };
        info.ArgumentList.Add(path); info.ArgumentList.Add("/grant");
        info.ArgumentList.Add("*"+sid+":"+(recursive ? "(OI)(CI)" : "")+(writable ? "(M)" : "(RX)"));
        if(recursive) info.ArgumentList.Add("/T");
        info.ArgumentList.Add("/Q");
        using var proc=Process.Start(info)!;
        var stdout=proc.StandardOutput.ReadToEndAsync();
        var stderr=proc.StandardError.ReadToEndAsync();
        if(!proc.WaitForExit(120000)) { proc.Kill(true); throw new InvalidOperationException("acl_timeout"); }
        Task.WaitAll(stdout,stderr);
        if(proc.ExitCode!=0) throw new InvalidOperationException("acl_failed");
    }
    static object Execute(JsonElement request) {
        string root=Path.GetFullPath(request.GetProperty("root").GetString()!);
        string directory=Path.GetFullPath(request.GetProperty("directory").GetString()!);
        string executable=Path.GetFullPath(request.GetProperty("executable").GetString()!);
        Require(executable.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase) ||
            executable.StartsWith(directory+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase));
        string profile="StudyClassroom."+Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(root)))[..24];
        IntPtr sid=IntPtr.Zero, attrs=IntPtr.Zero, capabilityPtr=IntPtr.Zero, environment=IntPtr.Zero, handles=IntPtr.Zero;
        IntPtr job=IntPtr.Zero, output=IntPtr.Zero, input=IntPtr.Zero, owner=IntPtr.Zero; ProcessInfo child=default;
        string resultFile=Path.Combine(directory,".output-"+Guid.NewGuid().ToString("N"));
        try {
            int hr=CreateAppContainerProfile(profile,profile,"Isolated classroom execution",IntPtr.Zero,0,out sid);
            if(hr!=0) Require(DeriveAppContainerSidFromAppContainerName(profile,out sid)==0);
            Require(sid!=IntPtr.Zero);
            Require(ConvertSidToStringSidW(sid,out var sidText));
            string sidString=Marshal.PtrToStringUni(sidText)!; LocalFree(sidText);
            phase="acl";
            string aclMarker=Path.Combine(root,".sandbox-acl-ready");
            if(!File.Exists(aclMarker) || File.ReadAllText(aclMarker)!=profile) {
                Grant(root,sidString,false,true);
                File.WriteAllText(aclMarker,profile);
            }
            Grant(directory,sidString,true,true);
            Directory.CreateDirectory(Path.Combine(directory,"Packages",profile.ToLowerInvariant(),"AC","Temp"));
            phase="attributes";
            var caps=new SecurityCapabilities{sid=sid};
            capabilityPtr=Marshal.AllocHGlobal(Marshal.SizeOf<SecurityCapabilities>());
            Marshal.StructureToPtr(caps,capabilityPtr,false);
            IntPtr bytes=IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero,2,0,ref bytes);
            attrs=Marshal.AllocHGlobal(bytes);
            Require(InitializeProcThreadAttributeList(attrs,2,0,ref bytes));
            Require(UpdateProcThreadAttribute(attrs,0,(IntPtr)0x00020009,capabilityPtr,
                (IntPtr)Marshal.SizeOf<SecurityCapabilities>(),IntPtr.Zero,IntPtr.Zero));
            job=CreateJobObjectW(IntPtr.Zero,IntPtr.Zero); Require(job!=IntPtr.Zero);
            owner=OpenProcess(0x100000,false,request.GetProperty("owner").GetUInt32());
            Require(owner!=IntPtr.Zero);
            phase="job";
            var limits=new ExtendedLimits {
                basic=new BasicLimits{flags=0x2000|0x200|0x8,processes=32},
                jobMemory=(UIntPtr)(1536UL*1024*1024)
            };
            Require(SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf<ExtendedLimits>()));
            var security=new SecurityAttributes{length=Marshal.SizeOf<SecurityAttributes>(),inherit=1};
            output=CreateFileW(resultFile,0x40000000,1,ref security,2,0,IntPtr.Zero);
            input=CreateFileW("NUL",0x80000000,1|2,ref security,3,0,IntPtr.Zero);
            Require(output!=(IntPtr)(-1) && input!=(IntPtr)(-1));
            handles=Marshal.AllocHGlobal(IntPtr.Size*2);
            Marshal.WriteIntPtr(handles,0,input);
            Marshal.WriteIntPtr(handles,IntPtr.Size,output);
            Require(UpdateProcThreadAttribute(attrs,0,(IntPtr)0x00020002,handles,
                (IntPtr)(IntPtr.Size*2),IntPtr.Zero,IntPtr.Zero));
            var startup=new StartupInfoEx {
                info=new StartupInfo{cb=Marshal.SizeOf<StartupInfoEx>(),flags=0x100,input=input,output=output,error=output},
                attributes=attrs
            };
            var environmentValues=new SortedDictionary<string,string>(StringComparer.OrdinalIgnoreCase);
            foreach(string name in new[]{"SystemRoot","WINDIR","COMSPEC","ProgramFiles","ProgramFiles(x86)",
                "CommonProgramFiles","ProgramData","ALLUSERSPROFILE"}) {
                var value=Environment.GetEnvironmentVariable(name);
                if(value!=null) environmentValues[name]=value;
            }
            foreach(string name in new[]{"TEMP","TMP","HOME","USERPROFILE","APPDATA","LOCALAPPDATA","MPLCONFIGDIR"})
                environmentValues[name]=directory;
            environmentValues["USERNAME"]="classroom";
            environmentValues["PATH"]=Path.Combine(root,"mingw","bin")+";"+Path.Combine(root,"jdk","bin")+";"+
                Environment.GetFolderPath(Environment.SpecialFolder.System);
            foreach(string name in new[]{"PYTHONNOUSERSITE","PYTHONDONTWRITEBYTECODE","OMP_NUM_THREADS","MKL_NUM_THREADS","OPENBLAS_NUM_THREADS"})
                environmentValues[name]="1";
            environmentValues["PYTHONIOENCODING"]="utf-8";
            string env=string.Join("\0",environmentValues.Select(pair=>pair.Key+"="+pair.Value))+"\0\0";
            environment=Marshal.StringToHGlobalUni(env);
            string arguments=Quote(executable)+" "+string.Join(" ",request.GetProperty("args").EnumerateArray().Select(x=>Quote(x.GetString()!)));
            phase="create";
            Require(CreateProcessW(executable,new StringBuilder(arguments),IntPtr.Zero,IntPtr.Zero,true,
                0x80000|0x400|0x4|0x08000000,environment,directory,ref startup,out child));
            Require(OpenProcessToken(child.process,8,out var token));
            try { Require(GetTokenInformation(token,29,out int isolated,4,out _) && isolated==1); }
            finally { CloseHandle(token); }
            if(!AssignProcessToJobObject(job,child.process)) { TerminateProcess(child.process,1); throw new InvalidOperationException(); }
            phase="execute";
            ResumeThread(child.thread);
            var timer=Stopwatch.StartNew();
            string status="error";
            while(WaitForSingleObject(child.process,50)==0x102) {
                if(timer.Elapsed.TotalSeconds>30 || new FileInfo(resultFile).Length>32768 ||
                    WaitForSingleObject(owner,0)==0) {
                    TerminateJobObject(job,1); status="stopped"; break;
                }
            }
            Require(GetExitCodeProcess(child.process,out uint code));
            if(code==0 && status!="stopped") status="success";
            TerminateJobObject(job,1); WaitForSingleObject(child.process,5000);
            CloseHandle(output); output=IntPtr.Zero;
            using var stream=File.OpenRead(resultFile);
            byte[] content=new byte[Math.Min(32768,stream.Length)];
            stream.ReadExactly(content);
            return new{status,output=Encoding.UTF8.GetString(content)};
        } finally {
            if(job!=IntPtr.Zero) { TerminateJobObject(job,1); CloseHandle(job); }
            if(child.process!=IntPtr.Zero) TerminateProcess(child.process,1);
            foreach(var handle in new[]{child.thread,child.process,output,input,owner}) if(handle!=IntPtr.Zero) CloseHandle(handle);
            if(attrs!=IntPtr.Zero) { DeleteProcThreadAttributeList(attrs); Marshal.FreeHGlobal(attrs); }
            if(capabilityPtr!=IntPtr.Zero) Marshal.FreeHGlobal(capabilityPtr);
            if(handles!=IntPtr.Zero) Marshal.FreeHGlobal(handles);
            if(environment!=IntPtr.Zero) Marshal.FreeHGlobal(environment);
            if(sid!=IntPtr.Zero) FreeSid(sid);
        }
    }
    static int Supervise() {
        using var document=JsonDocument.Parse(Console.ReadLine()!);
        var request=document.RootElement;
        string executable=request.GetProperty("executable").GetString()!;
        string directory=request.GetProperty("directory").GetString()!;
        IntPtr read=IntPtr.Zero,write=IntPtr.Zero,job=IntPtr.Zero,owner=IntPtr.Zero;
        ProcessInfo child=default; bool stopping=false;
        void CloseInput() {
            IntPtr handle=Interlocked.Exchange(ref write,IntPtr.Zero);
            if(handle!=IntPtr.Zero) CloseHandle(handle);
            Volatile.Write(ref stopping,true);
        }
        try {
            var attributes=new SecurityAttributes{length=Marshal.SizeOf<SecurityAttributes>(),inherit=1};
            Require(CreatePipe(out read,out write,ref attributes,0));
            Require(SetHandleInformation(write,1,0));
            job=CreateJobObjectW(IntPtr.Zero,IntPtr.Zero); Require(job!=IntPtr.Zero);
            var limits=new ExtendedLimits{basic=new BasicLimits{flags=0x2000}};
            Require(SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf<ExtendedLimits>()));
            owner=OpenProcess(0x100000,false,request.GetProperty("owner").GetUInt32());
            Require(owner!=IntPtr.Zero);
            var startup=new StartupInfoEx{info=new StartupInfo{
                cb=Marshal.SizeOf<StartupInfo>(),flags=0x100,input=read,
                output=GetStdHandle(-11),error=GetStdHandle(-12)}};
            Require(SetHandleInformation(startup.info.output,1,1));
            Require(SetHandleInformation(startup.info.error,1,1));
            var arguments=new StringBuilder(Quote(executable)+" "+string.Join(" ",
                request.GetProperty("args").EnumerateArray().Select(value=>Quote(value.GetString()!))));
            Require(CreateProcessW(executable,arguments,IntPtr.Zero,IntPtr.Zero,true,
                0x4|0x08000000,IntPtr.Zero,directory,ref startup,out child));
            Require(AssignProcessToJobObject(job,child.process));
            ResumeThread(child.thread);
            byte[] payload=Encoding.UTF8.GetBytes(request.GetProperty("payload").GetRawText()+"\n");
            Require(WriteFile(write,payload,(uint)payload.Length,out uint written,IntPtr.Zero) && written==payload.Length);
            _=Task.Run(()=>{Console.In.ReadToEnd();CloseInput();});
            var grace=new Stopwatch();
            while(WaitForSingleObject(child.process,100)==0x102) {
                if(WaitForSingleObject(owner,0)==0) CloseInput();
                if(Volatile.Read(ref stopping) && !grace.IsRunning) grace.Start();
                if(grace.Elapsed.TotalSeconds>35) break;
            }
            return 0;
        } finally {
            CloseInput();
            if(job!=IntPtr.Zero) { TerminateJobObject(job,1);CloseHandle(job); }
            if(child.process!=IntPtr.Zero) TerminateProcess(child.process,1);
            foreach(var handle in new[]{read,child.process,child.thread,owner}) if(handle!=IntPtr.Zero) CloseHandle(handle);
        }
    }
    static int Main(string[] args) {
        try {
            Console.InputEncoding=Encoding.UTF8;
            Console.OutputEncoding=new UTF8Encoding(false);
            if(args.Length==1 && args[0]=="--supervise") return Supervise();
            string input=Console.In.ReadToEnd();
            if(input.Length>100000) throw new InvalidOperationException();
            using var document=JsonDocument.Parse(input);
            Console.WriteLine(JsonSerializer.Serialize(Execute(document.RootElement)));
            return 0;
        } catch(Exception error) {
            Console.WriteLine(JsonSerializer.Serialize(new{status="error",output="隔离运行环境不可用。",
                isolation_failed=true,phase,error=error is InvalidOperationException ? error.Message : "internal"}));
            return 1;
        }
    }
}
